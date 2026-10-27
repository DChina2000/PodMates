"""看图能力检测：画一张写着三位数字的小图，让模型把数字读出来。会真调一次接口（一张小图，几百 token）。
面板里服务商卡片的「测试看图」和 tools/vision_check.py 都用这里。"""
import base64
import io
import random


def make_image(digits: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (360, 140), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 96)
    except OSError:
        font = ImageFont.load_default(size=96)
    d.text((24, 12), digits, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def check(llm, ep: dict, key: str, model: str, digits: str = "") -> dict:
    """ep 是服务商卡片（含 protocol、provider、base_url）。返回 {ok, expected, answer, usage}。"""
    digits = digits or "".join(random.choice("0123456789") for _ in range(3))
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(make_image(digits)).decode()}},
        {"type": "text", "text": "图片里写着几个数字？只回答数字，不要别的字。"}]}]
    url, headers, body = llm.build(ep, key, model, msgs, 0.0, "", 0)
    data = await llm._post(url, headers, body, llm.proxy_for(ep))
    text, usage = llm.parse(ep.get("protocol", "openai"), data)
    return {"ok": digits in "".join(c for c in text if c.isdigit()), "expected": digits, "answer": text[:80], "usage": usage}
