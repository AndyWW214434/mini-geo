"""The six platforms Bright Data's AI scrapers support today. One class each."""

from __future__ import annotations

from typing import Any

from geo.platforms.base import Extracted, PlatformAdapter, url_list


class ChatGPT(PlatformAdapter):
    platform_id = "chatgpt"
    name = "ChatGPT"
    hosts = ("chatgpt.com", "chat.openai.com")
    target_url = "https://chatgpt.com/"
    default_dataset_id = "gd_m7aof0k82r803d5bjm"

    def extract(self, r: dict[str, Any]) -> Extracted:
        asked, answered = self.prompts(r)
        cites = url_list(r.get("citations"), "url") + url_list(r.get("links_attached"), "url")
        return Extracted(r.get("answer_text_markdown") or r.get("answer_text"), cites,
                         self.common_error(r), asked, answered)


class Perplexity(PlatformAdapter):
    platform_id = "perplexity"
    name = "Perplexity"
    hosts = ("perplexity.ai",)
    target_url = "https://www.perplexity.ai/"
    default_dataset_id = "gd_m7dhdot1vw9a7gc1n"

    def extract(self, r: dict[str, Any]) -> Extracted:
        asked, answered = self.prompts(r)
        cites = url_list(r.get("sources"), "link", "url") + url_list(r.get("citations"), "url", "link")
        return Extracted(r.get("answer_text_markdown") or r.get("answer_text"), cites,
                         self.common_error(r), asked, answered)


class Gemini(PlatformAdapter):
    platform_id = "gemini"
    name = "Gemini"
    hosts = ("gemini.google.com",)
    target_url = "https://gemini.google.com/"
    default_dataset_id = "gd_mbz66arm2mf9cu856y"

    def extract(self, r: dict[str, Any]) -> Extracted:
        asked, answered = self.prompts(r)
        cites = url_list(r.get("links"), "url", "href") + url_list(r.get("citations"), "url")
        return Extracted(r.get("response_markdown") or r.get("answer_text"), cites,
                         self.common_error(r), asked, answered)


class GoogleAIMode(PlatformAdapter):
    platform_id = "google_ai_mode"
    name = "Google AI Mode"
    hosts = ("google.com",)
    target_url = "https://www.google.com/"
    default_dataset_id = "gd_mcswdt6z2elth3zqr2"

    def extract(self, r: dict[str, Any]) -> Extracted:
        asked, answered = self.prompts(r)
        cites = url_list(r.get("aio_citations"), "url", "link")
        return Extracted(r.get("aio_text") or r.get("answer_text"), cites, self.common_error(r),
                         asked, answered, r.get("is_truncated"))


class Copilot(PlatformAdapter):
    platform_id = "copilot"
    name = "Microsoft Copilot"
    hosts = ("copilot.microsoft.com",)
    target_url = "https://copilot.microsoft.com/"
    default_dataset_id = "gd_m7di5jy6s9geokz8w"

    def extract(self, r: dict[str, Any]) -> Extracted:
        asked, answered = self.prompts(r)
        cites = url_list(r.get("sources"), "url", "link")
        return Extracted(r.get("answer_text"), cites, self.common_error(r), asked, answered)


class Grok(PlatformAdapter):
    platform_id = "grok"
    name = "Grok"
    hosts = ("grok.com", "x.com")
    target_url = "https://grok.com/"
    default_dataset_id = "gd_m8ve0u141icu75ae74"

    def extract(self, r: dict[str, Any]) -> Extracted:
        asked, answered = self.prompts(r)
        resp = r.get("response") if isinstance(r.get("response"), dict) else {}
        text = resp.get("text") if resp else r.get("answer_text")
        cites = url_list(resp.get("web_results"), "url") + url_list(resp.get("x_posts"), "url")
        return Extracted(text, cites, self.common_error(r), asked, answered)
