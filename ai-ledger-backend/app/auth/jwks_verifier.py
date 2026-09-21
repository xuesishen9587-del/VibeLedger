"""Pinned issuer/key source with bounded, shared public-key caching."""
import json
import threading
import time
from urllib.parse import urlsplit
import jwt
import requests
from app.domain.auth import InvalidCredentialsError


class JWKSBrowserAuthVerifier:
    def __init__(self, url, issuer, audience, algorithms, *, fetch=None, clock=time.monotonic):
        parsed=urlsplit(issuer or "")
        if (parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path!="/auth/v1" or url!=(issuer or "")+"/.well-known/jwks.json"
                or not audience or not algorithms or not set(algorithms)<= {"RS256","ES256"}):
            raise InvalidCredentialsError("Pinned asymmetric authentication configuration is required.")
        self.url,self.issuer,self.audience,self.algorithms=url,issuer,audience,tuple(algorithms)
        self.fetch=fetch or self._fetch
        self.clock=clock
        self.keys={}
        self.expires_at=0
        self.last_attempt=float("-inf")
        self.last_unknown=float("-inf")
        self.lock=threading.Lock()

    def _fetch(self):
        with requests.get(self.url,timeout=5,allow_redirects=False,stream=True) as response:
            if response.status_code!=200:
                raise ValueError("Key source unavailable")
            content=bytearray()
            for chunk in response.iter_content(8192):
                content.extend(chunk)
                if len(content)>1024*1024:
                    raise ValueError("Key response too large")
            return json.loads(content)

    def _refresh(self, now):
        self.last_attempt=now
        document=self.fetch()
        entries=document.get("keys") if isinstance(document,dict) else None
        if not isinstance(entries,list) or not 1<=len(entries)<=32:
            raise ValueError("Invalid key set")
        keys={}
        for entry in entries:
            kid=entry.get("kid")
            if not isinstance(kid,str) or not kid or len(kid)>256 or kid in keys:
                raise ValueError("Invalid key identity")
            if entry.get("kty") not in ("RSA","EC") or entry.get("use","sig")!="sig" or "verify" not in entry.get("key_ops",["verify"]):
                continue
            key=jwt.PyJWK.from_dict(entry)
            if key.algorithm_name in self.algorithms:
                keys[kid]=key
        self.keys=keys
        self.expires_at=now+300

    def verify(self, token):
        try:
            if not isinstance(token,str) or len(token)>16384:
                raise ValueError("Invalid token")
            header=jwt.get_unverified_header(token)
            kid,algorithm=header.get("kid"),header.get("alg")
            if not isinstance(kid,str) or not kid or len(kid)>256 or algorithm not in self.algorithms:
                raise ValueError("Invalid token header")
            with self.lock:
                now=self.clock()
                refreshed=False
                if now>=self.expires_at:
                    if now-self.last_attempt<30:
                        raise ValueError("Key source retry bounded")
                    self._refresh(now)
                    refreshed=True
                if kid not in self.keys and not refreshed and now-self.last_unknown>=30:
                    self.last_unknown=now
                    self._refresh(now)
                key=self.keys.get(kid)
                if not key or key.algorithm_name!=algorithm:
                    raise ValueError("Unknown signing key")
            claims=jwt.decode(token,key.key,algorithms=list(self.algorithms),issuer=self.issuer,audience=self.audience,
                options={"require":["sub","exp","iss","aud"],"verify_signature":True,"verify_exp":True,"verify_nbf":True})
            if not isinstance(claims.get("sub"),str) or not claims["sub"].strip():
                raise ValueError("Missing subject")
            return claims
        except Exception:
            # Never expose tokens, provider responses, key material or network URLs.
            raise InvalidCredentialsError("Invalid or expired browser credentials.") from None
