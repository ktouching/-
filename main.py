from __future__ import annotations

import re
import unicodedata

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PUNCT_RE = re.compile(r"[\s\u200b-\u200f\ufe00-\ufe0f，。、！？；：,.!?;:~～\-—_·•…（）()【】\[\]「」『』\"'’“”]")
HANZI_RE = re.compile(r"[\u4e00-\u9fff]")


@register(
    "astrbot_plugin_wenziyu",
    "ktouching",
    "称呼强制 + 违禁词硬拦截（含拼音谐音），不依赖模型",
    "1.0.0",
)
class GuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

        raw_rules = str(self._cfg("address_rules", ""))
        self.address_rules: dict[str, str] = {}
        for line in raw_rules.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            user_id, title = line.split("|", 1)
            user_id, title = user_id.strip(), title.strip()
            if user_id and title:
                self.address_rules[user_id] = title

        raw_forbidden = str(self._cfg("forbidden_words", ""))
        self.forbidden: list[str] = [w.strip() for w in re.split(r"[\n,，]", raw_forbidden) if w.strip()]

        raw_pinyin = str(self._cfg("pinyin_patterns", "ajiweili,xilian"))
        self.pinyin_patterns: list[str] = [
            re.sub(r"[^a-z]", "", p.strip().lower()) for p in raw_pinyin.split(",") if p.strip()
        ]
        self.pinyin_patterns = [p for p in self.pinyin_patterns if p]

        self.forbidden_action = str(self._cfg("forbidden_action", "block")).strip().lower()
        self.block_inbound = bool(self._cfg("block_inbound", True))
        self.inbound_reply = str(self._cfg("inbound_reply", "我喜欢你"))
        self.outbound_block_reply = str(self._cfg("outbound_block_reply", "我喜欢你"))
        self.mask_with = str(self._cfg("mask_with", "***"))

        self._pinyin = None
        if self.pinyin_patterns:
            try:
                from pypinyin import Style, lazy_pinyin

                self._pinyin = (lazy_pinyin, Style)
                logger.info("[guard] 拼音谐音检测已启用：%s", self.pinyin_patterns)
            except Exception as exc:  # pragma: no cover
                logger.warning("[guard] pypinyin 不可用，只用字面词表：%s", exc)

        logger.info(
            "[guard] 称呼规则 %s；违禁词 %d 个；命中处理=%s；入站拦截=%s",
            self.address_rules,
            len(self.forbidden),
            self.forbidden_action,
            self.block_inbound,
        )

    def _cfg(self, key: str, default):
        getter = getattr(self.config, "get", None)
        if callable(getter):
            try:
                value = getter(key, default)
                return default if value is None else value
            except Exception:
                return default
        return default

    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text or "")
        return PUNCT_RE.sub("", text).lower()

    def _pinyin_hit(self, text: str) -> str | None:
        if not self._pinyin or not self.pinyin_patterns:
            return None
        lazy_pinyin, style = self._pinyin
        hanzi = "".join(HANZI_RE.findall(text))
        if not hanzi:
            return None
        try:
            joined = "".join(lazy_pinyin(hanzi, style=style.NORMAL, errors="ignore")).lower()
        except Exception:
            return None
        for pattern in self.pinyin_patterns:
            if pattern and pattern in joined:
                return pattern
        return None

    def _hit(self, text: str) -> str | None:
        if not text:
            return None
        normalized = self._normalize(text)
        for word in self.forbidden:
            if word and self._normalize(word) in normalized:
                return word
        for pattern in self.pinyin_patterns:
            if pattern and pattern in normalized:
                return f"拼音:{pattern}"
        pinyin_hit = self._pinyin_hit(text)
        if pinyin_hit:
            return f"拼音:{pinyin_hit}"
        return None

    def _mask(self, text: str) -> str:
        for word in self.forbidden:
            normalized_word = self._normalize(word)
            if not normalized_word:
                continue
            if normalized_word in self._normalize(text):
                text = re.sub(re.escape(word), self.mask_with, text)
        return text

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100000)
    async def inbound_guard(self, event: AstrMessageEvent):
        if not self.block_inbound:
            return
        try:
            text = event.get_message_str() or ""
        except Exception:
            return
        hit = self._hit(text)
        if not hit:
            return
        logger.info("[guard] 入站命中 %r，拦截 sender=%s", hit, event.get_sender_id())
        event.set_result(MessageEventResult().message(self.inbound_reply))
        event.stop_event()

    @filter.on_decorating_result(priority=-100000)
    async def outbound_guard(self, event: AstrMessageEvent):
        result = event.get_result()
        chain = list(getattr(result, "chain", []) or []) if result is not None else []
        if not chain:
            return

        full_text = "".join(
            str(getattr(c, "text", "") or "") for c in chain if isinstance(c, Plain)
        )
        hit = self._hit(full_text)
        if hit:
            logger.info("[guard] 出站命中 %r，处理=%s sender=%s", hit, self.forbidden_action, event.get_sender_id())
            if self.forbidden_action == "mask":
                for comp in chain:
                    if isinstance(comp, Plain):
                        comp.text = self._mask(str(getattr(comp, "text", "") or ""))
            else:
                event.set_result(MessageEventResult().message(self.outbound_block_reply))
                return

        title = self.address_rules.get(str(event.get_sender_id()))
        if title:
            first_plain = next((c for c in chain if isinstance(c, Plain)), None)
            already = False
            if first_plain is not None:
                head = str(getattr(first_plain, "text", "") or "").lstrip()
                already = head.startswith(title)
            if not already:
                if isinstance(chain[0], Plain):
                    chain[0].text = f"{title}，" + str(getattr(chain[0], "text", "") or "")
                else:
                    chain.insert(0, Plain(text=f"{title}，"))
                event.set_result(MessageEventResult().chain(chain))
                logger.info("[guard] 已给 %s 的回复加上称呼「%s」", event.get_sender_id(), title)
