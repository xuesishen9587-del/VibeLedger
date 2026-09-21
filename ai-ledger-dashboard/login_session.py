"""Supabase Auth tokens live only in one Streamlit server-side session."""
import math
import time
from urllib.parse import urlsplit
import requests


class LoginError(Exception):
    pass


class LoginUnavailable(LoginError):
    pass


class SupabasePasswordAuth:
    def __init__(self,url,key,post=None):
        parsed=urlsplit(url or "")
        if (parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("","/") or not key or not key.startswith("sb_publishable_")):
            raise LoginError("登录服务尚未配置，请联系管理员。")
        self.url=url.rstrip("/")+"/auth/v1"
        self.key=key
        self.post=post or requests.post

    def _request(self,path,body=None,params=None,access=None):
        headers={"apikey":self.key}
        if access:
            headers["Authorization"]="Bearer "+access
        try:
            response=self.post(self.url+path,json=body,params=params,headers=headers,timeout=10,allow_redirects=False)
            if response.status_code==429 or response.status_code>=500:
                raise LoginUnavailable("登录服务暂不可用，请稍后重试。")
            if not 200<=response.status_code<300:
                raise LoginError("登录未成功或会话已过期，请核对邮箱和密码。")
            return {} if response.status_code==204 else response.json()
        except (LoginError,LoginUnavailable):
            raise
        except Exception:
            raise LoginUnavailable("登录服务暂不可用，请稍后重试。") from None

    def sign_in(self,email,password):
        return self._request("/token",{"email":email,"password":password},{"grant_type":"password"})

    def refresh(self,token):
        return self._request("/token",{"refresh_token":token},{"grant_type":"refresh_token"})

    def sign_out(self,token):
        self._request("/logout",params={"scope":"local"},access=token)


class LoginSession:
    def __init__(self,state,provider,clock=time.time):
        self.state,self.provider,self.clock=state,provider,clock

    def _validated(self,payload):
        try:
            access,refresh=payload["access_token"],payload["refresh_token"]
            user=payload["user"]["id"]
            expires=float(payload.get("expires_at") or (self.clock()+float(payload["expires_in"])))
            if not all(isinstance(v,str) and v for v in (access,refresh,user)) or not math.isfinite(expires) or expires<=self.clock():
                raise ValueError()
            return {"access":access,"refresh":refresh,"subject":user,"expires":expires}
        except Exception:
            raise LoginUnavailable("登录服务返回无效会话，请重试。") from None

    def sign_in(self,email,password,authorize):
        session=self._validated(self.provider.sign_in(email,password))
        # Only the backend decides provisioned-user/household membership.
        try:
            authorize(session["access"])
        except Exception:
            try:
                self.provider.sign_out(session["access"])
            except LoginError:
                pass
            raise LoginError("暂时无法访问家庭账本，请联系管理员或稍后重试。") from None
        previous=self.state.get("_login",{}).get("subject") or self.state.get("_reauth_subject")
        if previous!=session["subject"]:
            self.state.clear()
        self.state["_login"]=session
        self.state.pop("_reauth_subject",None)

    def access_token(self):
        current=self.state.get("_login")
        if not current:
            return None
        if current["expires"]-self.clock()>60:
            return current["access"]
        try:
            new=self._validated(self.provider.refresh(current["refresh"]))
            if new["subject"]!=current["subject"]:
                raise LoginError("会话身份已改变，请重新登录。")
        except LoginUnavailable:
            # A timeout can have rotated the refresh token remotely. Retain it for
            # the provider's bounded reuse recovery; never discard financial retries.
            raise
        except LoginError:
            self.state["_reauth_subject"]=current["subject"]
            self.state.pop("_login",None)
            raise
        self.state["_login"]=new
        return new["access"]

    def sign_out(self):
        current=self.state.get("_login")
        success=True
        try:
            if current:
                self.provider.sign_out(current["access"])
        except LoginError:
            success=False
        finally:
            self.state.clear()
        return success
