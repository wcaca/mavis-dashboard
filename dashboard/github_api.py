"""
github_api.py - GitHub API 客户端 + 内存缓存

为 Mavis Dashboard 提供项目状态数据：
  - 用户所有 repo（own + collaborator）
  - 最近 commits / open issues / open PRs
  - 用户最近活动
  - 跨 repo 汇总统计

特性：
  - 5 分钟内存缓存（避免 GitHub rate limit）
  - 失败 retry（最多 2 次）
  - 异常隔离（单个 repo 失败不影响整体）
  - 部署 URL 映射（从 state/deployments.json 读取）
"""
import os
import json
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, List, Any


# Deployment mappings 路径
_DEPLOYMENTS_PATH_CANDIDATES = [
    os.environ.get("DEPLOYMENTS_FILE", ""),
    "/opt/mavis-dashboard/state/deployments.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "deployments.json"),
    os.path.join(os.getcwd(), "state", "deployments.json"),
]


GITHUB_API = "https://api.github.com"
DEFAULT_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CACHE_TTL = int(os.environ.get("GITHUB_CACHE_TTL", "300"))  # 5 分钟
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
MAX_WORKERS = 6


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [github_api] {msg}", flush=True)


# ============ Deployment mappings ============

_deployments_cache: Optional[Dict[str, str]] = None
_deployments_mtime: float = 0
_deployments_lock = threading.Lock()


def _find_deployments_file() -> Optional[str]:
    for p in _DEPLOYMENTS_PATH_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def _load_deployments() -> Dict[str, str]:
    """加载部署映射表，hot reload (修改 mtime 后重读)"""
    global _deployments_cache, _deployments_mtime
    with _deployments_lock:
        path = _find_deployments_file()
        if not path:
            _deployments_cache = {}
            _deployments_mtime = 0
            return {}
        try:
            mtime = os.path.getmtime(path)
            if _deployments_cache is not None and mtime == _deployments_mtime:
                return _deployments_cache
            with open(path) as f:
                data = json.load(f)
            # 过滤 _ 开头的 meta key
            result = {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}
            _deployments_cache = result
            _deployments_mtime = mtime
            log(f"加载部署映射 {len(result)} 条 ({path})")
            return result
        except Exception as e:
            log(f"⚠️  加载 deployments 失败: {e}")
            return _deployments_cache or {}


def get_deployment_url(full_name: str) -> Optional[str]:
    """查 repo 的部署 URL，full_name 严格小写"""
    if not full_name:
        return None
    mappings = _load_deployments()
    return mappings.get(full_name.lower())


class Cache:
    """线程安全的内存缓存"""

    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self._data: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            ts, data = entry
            if time.time() - ts > self.ttl:
                del self._data[key]
                self._misses += 1
                return None
            self._hits += 1
            return data

    def set(self, key: str, data: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), data)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._data),
                "hits": self._hits,
                "misses": self._misses,
                "ttl": self.ttl,
            }


class GitHubClient:
    """GitHub API 客户端"""

    def __init__(self, token: str = DEFAULT_TOKEN, cache_ttl: int = CACHE_TTL):
        self.token = token
        self.cache = Cache(ttl=cache_ttl)
        self._username_cache: Optional[str] = None
        self._username_lock = threading.Lock()

    # ---------- 底层 HTTP ----------

    def _request(self, url: str, params: Optional[dict] = None) -> dict:
        """GET 请求，带 auth + retry"""
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN 未配置")

        if params:
            url = url + "?" + urllib.parse.urlencode(params)

        last_err = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "Mavis-Dashboard/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")[:200]
                last_err = f"HTTP {e.code}: {body}"
                if e.code in (401, 403, 404):
                    # 这些不需要 retry
                    raise
                if e.code == 429:
                    # rate limited
                    retry_after = int(e.headers.get("Retry-After", "5"))
                    log(f"⚠️  rate limited, sleep {retry_after}s")
                    time.sleep(retry_after)
                else:
                    time.sleep(1 + attempt)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = str(e)
                time.sleep(1 + attempt)

        raise RuntimeError(f"GitHub API 失败（重试 {MAX_RETRIES} 次）: {last_err}")

    def _request_all_pages(self, url: str, params: Optional[dict] = None,
                            max_pages: int = 10) -> list:
        """分页拉取（每页 100）"""
        params = dict(params or {})
        params["per_page"] = 100
        all_items = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            items = self._request(url, params)
            if not items:
                break
            all_items.extend(items)
            if len(items) < 100:
                break
        return all_items

    # ---------- 业务 API ----------

    def get_username(self) -> str:
        """拿当前 token 对应的 username（缓存）"""
        with self._username_lock:
            if self._username_cache:
                return self._username_cache
            data = self._request(f"{GITHUB_API}/user")
            self._username_cache = data["login"]
            return self._username_cache

    def list_user_repos(self, username: Optional[str] = None,
                          include_forks: bool = False,
                          include_archived: bool = True) -> List[dict]:
        """列出用户所有 repo（own + collaborator）"""
        if not username:
            username = self.get_username()
        # 用 /user/repos 端点（会包含 private + collaborator + org）
        cache_key = f"repos:{username}:forks={include_forks}:archived={include_archived}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        items = self._request_all_pages(f"{GITHUB_API}/user/repos", {"sort": "updated"})
        # 筛选
        filtered = []
        for r in items:
            if not include_forks and r.get("fork"):
                continue
            if not include_archived and r.get("archived"):
                continue
            filtered.append(r)

        self.cache.set(cache_key, filtered)
        log(f"列 {len(filtered)} 个 repo（共 {len(items)} 条）")
        return filtered

    def get_repo(self, full_name: str) -> dict:
        """单个 repo 详情"""
        cache_key = f"repo:{full_name}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        data = self._request(f"{GITHUB_API}/repos/{full_name}")
        self.cache.set(cache_key, data)
        return data

    def get_repo_full(self, full_name: str, activity_limit: int = 20) -> dict:
        """单个 repo 详情 + 完整的 commits / issues / PRs 列表"""
        cache_key = f"repo_full:{full_name}:{activity_limit}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        # 并发拉
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_repo = ex.submit(self.get_repo, full_name)
            f_commits = ex.submit(self.get_recent_commits, full_name, per_page=activity_limit)
            f_issues = ex.submit(self.get_open_issues, full_name)
            f_prs = ex.submit(self.get_open_prs, full_name)
            repo = f_repo.result()
            commits = f_commits.result()
            issues = f_issues.result()
            prs = f_prs.result()

        # 格式化
        commit_items = []
        for c in commits:
            sha = c.get("sha", "")[:7]
            msg_lines = c.get("commit", {}).get("message", "").split("\n")
            msg = msg_lines[0]
            author = c.get("commit", {}).get("author", {}).get("name", "?")
            date = c.get("commit", {}).get("author", {}).get("date", "")
            commit_items.append({
                "sha": sha,
                "short_sha": sha[:7],
                "message": msg,
                "message_full": c.get("commit", {}).get("message", ""),
                "author": author,
                "author_username": c.get("author", {}).get("login", "") if c.get("author") else "",
                "date": date,
                "url": c.get("html_url", ""),
            })

        issue_items = []
        for i in issues:
            issue_items.append({
                "number": i.get("number"),
                "title": i.get("title", ""),
                "state": i.get("state", ""),
                "user": i.get("user", {}).get("login", "?"),
                "created_at": i.get("created_at", ""),
                "updated_at": i.get("updated_at", ""),
                "comments": i.get("comments", 0),
                "labels": [l.get("name", "") for l in i.get("labels", [])],
                "url": i.get("html_url", ""),
                "body": (i.get("body") or "")[:300],
            })

        pr_items = []
        for p in prs:
            pr_items.append({
                "number": p.get("number"),
                "title": p.get("title", ""),
                "state": p.get("state", ""),
                "user": p.get("user", {}).get("login", "?"),
                "created_at": p.get("created_at", ""),
                "updated_at": p.get("updated_at", ""),
                "draft": p.get("draft", False),
                "mergeable": p.get("mergeable"),
                "additions": p.get("additions"),
                "deletions": p.get("deletions"),
                "changed_files": p.get("changed_files"),
                "head_ref": p.get("head", {}).get("ref", ""),
                "base_ref": p.get("base", {}).get("ref", ""),
                "labels": [l.get("name", "") for l in p.get("labels", [])],
                "url": p.get("html_url", ""),
                "body": (p.get("body") or "")[:300],
            })

        result = {
            "repo": {
                "name": repo.get("name"),
                "full_name": repo.get("full_name"),
                "description": repo.get("description") or "",
                "private": repo.get("private", False),
                "fork": repo.get("fork", False),
                "archived": repo.get("archived", False),
                "language": repo.get("language"),
                "stars": repo.get("stargazers_count", 0),
                "forks": repo.get("forks_count", 0),
                "open_issues": repo.get("open_issues_count", 0),
                "watchers": repo.get("watchers_count", 0),
                "size_kb": repo.get("size", 0),
                "default_branch": repo.get("default_branch", "main"),
                "html_url": repo.get("html_url", ""),
                "clone_url": repo.get("clone_url", ""),
                "ssh_url": repo.get("ssh_url", ""),
                "pushed_at": repo.get("pushed_at", ""),
                "updated_at": repo.get("updated_at", ""),
                "created_at": repo.get("created_at", ""),
                "topics": repo.get("topics", []),
                "license": (repo.get("license") or {}).get("spdx_id", "") if repo.get("license") else "",
                "homepage": repo.get("homepage", ""),
                "deployment_url": get_deployment_url(repo.get("full_name", full_name)),
            },
            "commits": commit_items,
            "issues": issue_items,
            "pulls": pr_items,
            "stats": {
                "commits_count": len(commit_items),
                "open_issues_count": len(issue_items),
                "open_prs_count": len(pr_items),
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        self.cache.set(cache_key, result)
        return result

    def get_recent_commits(self, full_name: str, since: Optional[str] = None,
                             per_page: int = 5) -> List[dict]:
        """最近 commits（per_page 最大 100）"""
        if not since:
            since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cache_key = f"commits:{full_name}:{since}:{per_page}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        items = self._request_all_pages(
            f"{GITHUB_API}/repos/{full_name}/commits",
            {"since": since},
            max_pages=1,  # 限制只查 1 页（per_page=100）
        )[:per_page]
        self.cache.set(cache_key, items)
        return items

    def get_open_issues(self, full_name: str) -> List[dict]:
        """开放 issues（不含 PR）"""
        cache_key = f"issues:{full_name}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        # issues API 默认包含 PR，通过 filter 排除
        items = self._request_all_pages(
            f"{GITHUB_API}/repos/{full_name}/issues",
            {"state": "open", "filter": "all"},
            max_pages=2,
        )
        # 排除 PR（PR 也有 issues endpoint）
        issues = [i for i in items if "pull_request" not in i]
        self.cache.set(cache_key, issues)
        return issues

    def get_open_prs(self, full_name: str) -> List[dict]:
        """开放 PRs（用 pulls 端点）"""
        cache_key = f"prs:{full_name}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        items = self._request_all_pages(
            f"{GITHUB_API}/repos/{full_name}/pulls",
            {"state": "open", "sort": "updated", "direction": "desc"},
            max_pages=2,
        )
        self.cache.set(cache_key, items)
        return items

    def get_user_events(self, username: str, per_page: int = 30) -> List[dict]:
        """用户最近活动（push、PR、issue、star 等）"""
        cache_key = f"events:{username}:{per_page}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        # 公开端点，per_page 最大 100
        events = self._request(
            f"{GITHUB_API}/users/{username}/events/public",
            {"per_page": per_page},
        )
        self.cache.set(cache_key, events)
        return events

    def get_recent_activity_for_repo(self, full_name: str, per_page: int = 10) -> List[dict]:
        """单个 repo 的最近混合活动（commits + issues + PRs，按时间排序）"""
        cache_key = f"activity:{full_name}:{per_page}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        # 并发拉
        commits = self.get_recent_commits(full_name, per_page=per_page)
        issues = self.get_open_issues(full_name)[:per_page]
        prs = self.get_open_prs(full_name)[:per_page]

        items = []
        for c in commits:
            sha = c.get("sha", "")[:7]
            msg = c.get("commit", {}).get("message", "").split("\n")[0]
            author = c.get("commit", {}).get("author", {}).get("name", "?")
            date = c.get("commit", {}).get("author", {}).get("date", "")
            items.append({
                "type": "commit",
                "title": msg,
                "subtitle": f"{sha} by {author}",
                "url": c.get("html_url", ""),
                "date": date,
            })
        for i in issues:
            items.append({
                "type": "issue",
                "title": i.get("title", ""),
                "subtitle": f"#{i.get('number')} by {i.get('user', {}).get('login', '?')}",
                "url": i.get("html_url", ""),
                "date": i.get("created_at", ""),
                "state": i.get("state", ""),
            })
        for p in prs:
            items.append({
                "type": "pr",
                "title": p.get("title", ""),
                "subtitle": f"#{p.get('number')} by {p.get('user', {}).get('login', '?')}",
                "url": p.get("html_url", ""),
                "date": p.get("created_at", ""),
                "state": p.get("state", ""),
                "draft": p.get("draft", False),
            })
        # 按时间倒序
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        items = items[:per_page]
        self.cache.set(cache_key, items)
        return items

    # ---------- 聚合 API ----------

    def get_dashboard_data(self, username: Optional[str] = None,
                              include_forks: bool = False) -> dict:
        """生成 dashboard 数据：只拉 repos 列表（快 ~3s）。

        每个 repo 的 commits/issues/PRs 不在这里拉，按需走
        /api/repos/<name>（get_repo_full）懒加载。
        """
        if not username:
            username = self.get_username()
        start = time.time()

        repos = self.list_user_repos(username, include_forks=include_forks)
        total = len(repos)

        # 构轻量 list（不调 activity API）
        enriched: List[dict] = []
        archived_count = 0
        fork_count = 0
        total_stars = 0
        total_forks = 0
        language_count: Dict[str, int] = {}

        for r in repos:
            full_name = r["full_name"]
            item = {
                "name": r["name"],
                "full_name": full_name,
                "description": (r.get("description") or "")[:200],
                "private": r.get("private", False),
                "fork": r.get("fork", False),
                "archived": r.get("archived", False),
                "language": r.get("language"),
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "open_issues": r.get("open_issues_count", 0),
                "default_branch": r.get("default_branch", "main"),
                "html_url": r.get("html_url", ""),
                "pushed_at": r.get("pushed_at", ""),
                "updated_at": r.get("updated_at", ""),
                "created_at": r.get("created_at", ""),
                "topics": r.get("topics", [])[:5],
                "size_kb": r.get("size", 0),
                "deployment_url": get_deployment_url(full_name),
            }
            enriched.append(item)
            if item["archived"]:
                archived_count += 1
            if item["fork"]:
                fork_count += 1
            total_stars += item["stars"]
            total_forks += item["forks"]
            if item["language"]:
                language_count[item["language"]] = language_count.get(item["language"], 0) + 1

        # 按 pushed_at 倒序
        enriched.sort(key=lambda x: x.get("pushed_at", ""), reverse=True)

        # 算近 7/30 天活跃 repo
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        active_repos_7d = sum(1 for r in enriched if r.get("pushed_at", "") > seven_days_ago)
        active_repos_30d = sum(1 for r in enriched if r.get("pushed_at", "") > thirty_days_ago)

        # open issues / PRs 总量（GitHub list_user_repos 已经返回 open_issues_count
        # 但它包含 PR — 我们不能区分。粗略估算：用 list 提供的 open_issues_count 总和
        # 不准，所以标 None，让前端别显示具体数字
        open_issues_total = sum(item["open_issues"] for item in enriched)
        # 推算 PR 数量（无法准确，标 0）
        open_prs_total = 0
        commits_today = 0
        commits_7d = 0

        # 汇总 user 事件
        try:
            user_events = self.get_user_events(username, per_page=30)
        except Exception:
            user_events = []

        # 从 user_events 算 commits 今日/7天、PR 数、issue 数
        # user_events 是 30 条按时间倒序的事件，可能不够覆盖所有，
        # 但作为“快”指标足够
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        seven_days_ago_dt = datetime.now(timezone.utc) - timedelta(days=7)
        for ev in (user_events or []):
            created = ev.get("created_at", "")
            ev_type = ev.get("type", "")
            if not created:
                continue
            try:
                ev_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                continue
            if ev_type == "PushEvent":
                commits_in = (ev.get("payload") or {}).get("commits") or []
                if created.startswith(today_str):
                    commits_today += len(commits_in)
                if ev_dt > seven_days_ago_dt:
                    commits_7d += len(commits_in)
            elif ev_type == "PullRequestEvent" and (ev.get("payload") or {}).get("action") == "opened":
                open_prs_total += 1
            elif ev_type == "IssuesEvent" and (ev.get("payload") or {}).get("action") == "opened":
                open_issues_total += 1

        return {
            "user": {
                "login": username,
                "html_url": f"https://github.com/{username}",
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int((time.time() - start) * 1000),
            "stats": {
                "total_repos": total,
                "private_repos": sum(1 for r in enriched if r["private"]),
                "public_repos": sum(1 for r in enriched if not r["private"]),
                "archived": archived_count,
                "forks": fork_count,
                "total_stars": total_stars,
                "total_forks": total_forks,
                "open_issues": open_issues_total,
                "open_prs": open_prs_total,
                "active_repos_7d": active_repos_7d,
                "active_repos_30d": active_repos_30d,
                "commits_today": commits_today,
                "commits_7d": commits_7d,
                "languages": dict(sorted(language_count.items(), key=lambda x: -x[1])[:10]),
            },
            "repos": enriched,
            "user_events": user_events[:20],
            "cache": self.cache.stats(),
        }


# 模块级单例
_client: Optional[GitHubClient] = None
_client_lock = threading.Lock()


def get_client() -> GitHubClient:
    """获取全局单例 client"""
    global _client
    with _client_lock:
        if _client is None:
            _client = GitHubClient()
        return _client
