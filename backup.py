# -*- coding: utf-8 -*-
"""GitHub auto-backup / auto-restore using only the standard library."""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token, repo, branch="main", path="data/data.json", api="https://api.github.com"):
        self.token = token
        self.repo = repo.strip().strip("/")
        self.branch = branch or "main"
        self.path = path.strip().lstrip("/")
        self.api = api.rstrip("/")
        self._sha = {}

    def _call(self, method, url, body=None, accept="application/vnd.github+json"):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "zad-file-store-bot")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            raise GitHubError("network: %s" % e)

    def _url(self, path):
        return "%s/repos/%s/contents/%s" % (self.api, self.repo, urllib.parse.quote(path))

    def get(self, path=None):
        """Return (text or None if file does not exist). Raises GitHubError on other failures."""
        path = path or self.path
        url = self._url(path) + "?ref=" + urllib.parse.quote(self.branch)
        code, body = self._call("GET", url)
        if code == 404:
            self._sha.pop(path, None)
            return None
        if code != 200:
            raise GitHubError("GET %s -> %s %s" % (path, code, body[:200]))
        j = json.loads(body.decode("utf-8"))
        self._sha[path] = j.get("sha")
        enc = j.get("content") or ""
        if enc.strip() and j.get("encoding") == "base64":
            return base64.b64decode(enc).decode("utf-8")
        code, raw = self._call("GET", url, accept="application/vnd.github.raw")
        if code != 200:
            raise GitHubError("RAW %s -> %s" % (path, code))
        return raw.decode("utf-8")

    def put(self, text, message, path=None):
        path = path or self.path
        for attempt in range(2):
            if path not in self._sha:
                code, body = self._call("GET", self._url(path) + "?ref=" + urllib.parse.quote(self.branch))
                if code == 200:
                    self._sha[path] = json.loads(body.decode("utf-8")).get("sha")
                elif code != 404:
                    raise GitHubError("SHA %s -> %s" % (path, code))
            payload = {
                "message": message,
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "branch": self.branch,
            }
            if self._sha.get(path):
                payload["sha"] = self._sha[path]
            code, body = self._call("PUT", self._url(path), payload)
            if code in (200, 201):
                self._sha[path] = json.loads(body.decode("utf-8"))["content"]["sha"]
                return True
            if code in (409, 422) and attempt == 0:
                self._sha.pop(path, None)
                continue
            raise GitHubError("PUT %s -> %s %s" % (path, code, body[:200]))
        return False
