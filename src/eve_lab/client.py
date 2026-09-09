"""Cookie-authenticated EVE-NG REST client."""

import json
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class EveAPIError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = int(code) if str(code).isdigit() else None


class EveClient:
    def __init__(self, url: str, timeout: int = 15):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            self.url + "/api/" + path.lstrip("/"), data=body, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            detail = ""
            try:
                response = json.load(error)
                if isinstance(response, dict) and isinstance(response.get("message"), str):
                    detail = ": " + response["message"]
            except (ValueError, OSError):
                pass
            raise EveAPIError(f"EVE-NG returned HTTP {error.code} for {path}{detail}", error.code) from None
        except TimeoutError:
            raise RuntimeError(
                f"Timed out after {self.timeout}s during {method} {path}; "
                "the server may still be processing the request. Inspect remote state before retrying"
            ) from None
        except URLError as error:
            raise RuntimeError(f"Cannot reach EVE-NG at {self.url} during {method} {path}: {error.reason}") from None
        except ValueError:
            raise RuntimeError("EVE-NG returned invalid JSON") from None
        if not isinstance(result, dict) or result.get("status") != "success":
            message = result.get("message", "Unknown API error") if isinstance(result, dict) else "Invalid response"
            raise EveAPIError(f"EVE-NG: {message}", result.get("code") if isinstance(result, dict) else None)
        return result.get("data")

    def login(self, username: str, password: str):
        self.request("POST", "auth/login", {"username": username, "password": password})

    def logout(self):
        self.request("GET", "auth/logout")
