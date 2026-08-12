import base64
import requests
import sys

# 本地 FastAPI 运行地址
LOCAL_URL = "http://127.0.0.1:8000/api/record"

# 获取一张真实的 200x200 JPEG 图像的 Base64 编码，确保通过 Gemini API 的图像校验
def get_mock_image_base64():
    fallback_base64 = (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
        "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh"
        "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR"
        "AFAAUADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
        "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
        "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
        "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
        "5ufo6erx8vP09fb3+Pn6/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMi"
        "MoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldW"
        "Wl5iZmqociGgpOlJigbKKy8wMS4wYDVDAGQ5KokNJDw4Q0FHSE9LDFBhJGlNY2RPVFdYWVpj"
        "dGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
        "xcGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oAMBAAIRAxEAPwD3+iii"
        "gAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKA"
        "CiiigD//2Q=="
    )
    try:
        import urllib.request
        # 从 stable 公共地址获取一张 200x200 的随机真图
        url = "https://picsum.photos/200"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return base64.b64encode(response.read()).decode('utf-8')
    except Exception as e:
        print(f"⚠️ 无法从网络下载测试图，使用预设备用图 (原因: {e})")
        return fallback_base64

MOCK_IMAGE_BASE64 = get_mock_image_base64()

def test_api(note):
    payload = {
        "image": MOCK_IMAGE_BASE64,
        "note": note
    }
    
    print(f"⏳ 正在向本地 FastAPI 发送模拟请求...")
    print(f"📝 备注内容: {note}")
    try:
        response = requests.post(LOCAL_URL, json=payload, timeout=15)
        if response.status_code == 200:
            print("✅ 接口请求成功！后端返回数据：")
            print(response.json())
        else:
            print(f"❌ 接口返回错误码 {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ 无法连接到本地服务: {e}")
        print("💡 请确认您的 FastAPI 后端已在 8000 端口启动 (uvicorn main:app --reload)")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        user_note = " ".join(sys.argv[1:])
    else:
        # 默认测试案例：测试多账户转账与汇率损益
        user_note = "测试跨币种转账还款：从 ICBC_Debit 购汇还款给 ICBC_Visa_Credit 100 USD，实际扣除 725元"
        
    test_api(user_note)
