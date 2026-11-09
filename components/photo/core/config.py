"""Validate native settings once, without rewriting uploaded file values."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    generate_enabled: bool = True
    edit_enabled: bool = True
    selfie_enabled: bool = True
    base_url: str = ""
    api_keys: tuple[str, ...] = ()
    model: str = ""

    @classmethod
    def from_config(cls, config):
        flags = {}
        for key in ("generate_enabled", "edit_enabled", "selfie_enabled"):
            value = config.get(key, True)
            if not isinstance(value, bool):
                raise ValueError("功能开关必须是开启或关闭")
            flags[key] = value
        keys = config.get("api_keys", [])
        if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
            raise ValueError("密钥应逐项填写")
        text = {}
        for key in ("base_url", "model"):
            value = config.get(key, "")
            if not isinstance(value, str):
                raise ValueError("服务地址和模型名称应填写文字")
            text[key] = value.strip()
        return cls(
            **flags,
            api_keys=tuple(k.strip() for k in keys if k.strip()),
            **text,
        )

    @property
    def configured(self):
        return bool(self.base_url and self.api_keys and self.model)

    @property
    def enabled(self):
        return self.generate_enabled or self.edit_enabled or self.selfie_enabled


def validate_request(
    user_prompt, photo_type, count, with_user_image, aspect_ratio, mode
):
    from .planner import SIZE_MAP

    if not isinstance(user_prompt, str) or not user_prompt.strip():
        raise ValueError("请说明想拍或修改什么")
    if len(user_prompt) > 8000:
        raise ValueError("这次照片要求太长，请精简后重试")
    if not isinstance(photo_type, str) or photo_type not in {
        "selfie",
        "scene",
        "food",
        "outfit",
    }:
        raise ValueError("照片类型应为自拍、场景、食物或穿搭")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
        raise ValueError("一次可以拍一到四张照片")
    if not isinstance(with_user_image, bool):
        raise ValueError("合影选项必须是开启或关闭")
    if not isinstance(mode, str) or mode not in {"photo", "text", "edit"}:
        raise ValueError("照片模式应为生活照、普通生图或改图")
    if with_user_image and (mode != "photo" or photo_type not in {"selfie", "outfit"}):
        raise ValueError("合影请使用自拍或穿搭生活照")
    if not isinstance(aspect_ratio, str):
        raise ValueError("照片比例应填写如3:4")
    if aspect_ratio and aspect_ratio not in SIZE_MAP:
        raise ValueError("暂不支持这个比例，请使用3:4、4:3、1:1、16:9等常见比例")


def ordered_references_model(model):
    value = model.lower()
    return value.startswith("gpt-image-") or value == "chatgpt-image-latest"
