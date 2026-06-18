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


GITHUB_API = "https://api.github.com"
DEFAULT_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CACHE_TTL = int(os.environ.get("GITHUB_CACHE_TTL", "300"))  # 5 分钟
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
MAX_WORKERS = 6


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [github_api] {msg}", flush=True)


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
        """生成完整 dashboard 数据"""
        if not username:
            username = self.get_username()
        start = time.time()

        repos = self.list_user_repos(username, include_forks=include_forks)
        total = len(repos)

        # 并发拉每个 repo 的活动
        enriched: List[dict] = []
        activity_per_repo: Dict[str, list] = {}
        open_issues_total = 0
        open_prs_total = 0
        archived_count = 0
        fork_count = 0
        total_stars = 0
        total_forks = 0
        language_count: Dict[str, int] = {}

        def _enrich(r: dict) -> dict:
            full_name = r["full_name"]
            # 最近活动
            try:
                activity = self.get_recent_activity_for_repo(full_name, per_page=5)
            except Exception as e:
                activity = []
            return {
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
                "activity": activity,
            }

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(_enrich, r): r for r in repos}
            for fut in as_completed(futures):
                try:
                    item = fut.result()
                except Exception as e:
                    log(f"enrich 失败: {e}")
                    continue
                enriched.append(item)
                if item["archived"]:
                    archived_count += 1
                if item["fork"]:
                    fork_count += 1
                total_stars += item["stars"]
                total_forks += item["forks"]
                # 语言统计
                if item["language"]:
                    language_count[item["language"]] = language_count.get(item["language"], 0) + 1
                # 活动分类
                for a in item["activity"]:
                    if a["type"] == "issue":
                        open_issues_total += 1
                    elif a["type"] == "pr":
                        open_prs_total += 1

        # 按 pushed_at 倒序
        enriched.sort(key=lambda x: x.get("pushed_at", ""), reverse=True)

        # 算近 7 天 commits
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        active_repos_7d = sum(1 for r in enriched if r.get("pushed_at", "") > seven_days_ago)
        active_repos_30d = sum(1 for r in enriched if r.get("pushed_at", "") > (datetime.now(timezone.utc) - timedelta(days=30)).isoformat())

        # 算最近 commit 数量（每个 repo 的 activity 里有 commits）
        commits_today = 0
        commits_7d = 0
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for r in enriched:
            for a in r.get("activity", []):
                if a["type"] != "commit":
                    continue
                d = a.get("date", "")
                if d.startswith(today_str):
                    commits_today += 1
                if d > seven_days_ago:
                    commits_7d += 1

        # 汇总 user 事件
        try:
            user_events = self.get_user_events(username, per_page=30)
        except Exception:
            user_events = []

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
