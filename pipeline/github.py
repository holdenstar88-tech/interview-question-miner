"""Optional public GitHub REST API source; never scrapes GitHub search pages.

GitHub explicitly permits unauthenticated REST requests for public data. This
adapter only uses documented repository search/content/commit endpoints, checks
rate-limit headers and never supplies credentials or follows restricted links.
"""
from __future__ import annotations
import base64
import hashlib
import re
from datetime import timedelta
from urllib.parse import urlencode,quote,urlsplit
from .collect import RawPost
from .http import PublicHTTP,SkipPage,SourceUnavailable
from .runtime import now

class GitHubCollector:
    def __init__(self,cfg,days: int):
        self.app=cfg
        self.cfg=cfg.sources["github"]
        self.http=PublicHTTP(cfg.collect,["api.github.com"],self.cfg.max_requests_per_run)
        self.http.session.headers["Accept"]="application/vnd.github+json"
        self.since=(now().date()-timedelta(days=days-1)).isoformat()
        self.files={}

    def _api(self,path: str):
        if not path.startswith("/repos/") and not path.startswith("/search/repositories?"):
            raise SkipPage("非允许的 GitHub REST 端点")
        # This is a documented public API (not a robots-exempt web crawler).
        self.http.page_requests += 1
        response=self.http._send("https://api.github.com"+path)
        if response.status_code!=200:
            raise SkipPage(f"GitHub REST HTTP {response.status_code}")
        if response.headers.get("X-RateLimit-Remaining")=="0":
            raise SourceUnavailable("GitHub API 限额耗尽；停止，不使用其他身份重试")
        try:
            return response.json()
        except ValueError as exc:
            raise SkipPage("GitHub API 非 JSON 响应") from exc

    def discover(self):
        repos=list(self.cfg.repositories)
        if not repos:
            params=urlencode({"q":f"{self.cfg.search_query} pushed:>={self.since}","sort":"updated","order":"desc","per_page":max(1,self.cfg.max_discovery_pages)})
            data=self._api("/search/repositories?"+params)
            repos=[r["full_name"] for r in data.get("items",[]) if not r.get("private") and not r.get("archived")]
        queue=[(repo,"") for repo in repos if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repo)]
        for _ in range(self.cfg.max_discovery_pages):
            if not queue:break
            repo,path=queue.pop(0)
            entries=self._api(f"/repos/{repo}/contents/{quote(path,safe='/')}")
            if not isinstance(entries,list):continue
            for entry in entries:
                if entry.get("type")=="dir":
                    queue.append((repo,entry["path"]))
                elif entry.get("type")=="file" and entry.get("name","").lower().endswith(".md"):
                    url=entry.get("html_url","")
                    if urlsplit(url).hostname=="github.com":
                        self.files[url]=(repo,entry["path"])
        return list(self.files)[:self.app.collect.max_candidates_per_source]

    def fetch(self,url: str):
        repo,path=self.files[url]
        data=self._api(f"/repos/{repo}/contents/{quote(path,safe='/')}")
        if data.get("type")!="file" or data.get("encoding")!="base64" or data.get("size",0)>self.app.collect.max_response_bytes:
            raise SkipPage("非公开 Markdown 文件或文件过大")
        commits=self._api(f"/repos/{repo}/commits?"+urlencode({"path":path,"per_page":1}))
        if not commits:raise SkipPage("缺少文件发布时间")
        published=commits[0]["commit"]["committer"]["date"]
        try:
            content=base64.b64decode(data["content"]).decode("utf-8")
        except (ValueError,UnicodeError) as exc:
            raise SkipPage("Markdown 编码无效") from exc
        if any(word in content for word in ("付费后可阅读","购买后阅读","会员专享文章")):
            raise SkipPage("付费内容引用/预览，跳过")
        title=next((line.lstrip("# ").strip() for line in content.splitlines() if line.startswith("# ")),path)
        return RawPost(url,hashlib.sha256(url.encode()).hexdigest()[:24],title,repo.split("/")[0],published,now().isoformat(timespec="seconds"),content,"github")
