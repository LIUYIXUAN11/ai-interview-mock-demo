"""图片简历识别：用 macOS 自带的 Vision 框架（离线、免费、支持中英文）。

只做一件事：图片 → 文本。识别不出来时返回错误信息，由界面提示用户换 PDF/Word。
"""

import io
import os

SUPPORTED = (".jpg", ".jpeg", ".png")


def available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def image_to_text(data: bytes) -> tuple[str, str]:
    """返回 (文本, 错误信息)。"""
    if not available():
        return "", "这台机器没有可用的图片识别组件，请上传 PDF 或 Word。"

    try:
        import objc
        import Quartz
        import Vision
        from Foundation import NSData
    except Exception as e:  # pragma: no cover
        return "", f"图片识别组件加载失败：{e}"

    try:
        ns_data = NSData.dataWithBytes_length_(data, len(data))
        src = Quartz.CGImageSourceCreateWithData(ns_data, None)
        if src is None:
            return "", "这张图片读不出来（可能不是标准 JPG/PNG）。"
        image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if image is None:
            return "", "这张图片内容为空。"

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(0)          # 0 = accurate（1 是 fast，中文会糊）
        request.setUsesLanguageCorrection_(True)
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])

        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            return "", f"识别失败：{err}"

        lines = []
        for obs in (request.results() or []):
            cands = obs.topCandidates_(1)
            if cands and len(cands) > 0:
                lines.append(cands[0].string())

        text = "\n".join(l.strip() for l in lines if l.strip())
        text = _tidy(text)
        if len(text) < 30:
            return "", "识别出来的字太少，可能是图片太糊或不是简历。建议上传 PDF。"
        return text, ""
    except Exception as e:  # pragma: no cover
        return "", f"图片识别出错：{e}"


def _tidy(text: str) -> str:
    """把 OCR 结果整理得像简历的换行：去掉空行、合并明显断行。"""
    import re
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    out = []
    for l in lines:
        if out and len(out[-1]) < 24 and not re.search(r"[。；;：:]$", out[-1]):
            out[-1] = out[-1] + l          # 上一行太短，多半是被截断的句子
        else:
            out.append(l)
    return "\n".join(out)
