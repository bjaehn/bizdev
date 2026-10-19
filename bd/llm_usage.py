"""Token and web-search counters for one or more model calls (no SDK import)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    web_searches: int = 0
    # Tokens of requests whose prompt was longer than the model's long-prompt threshold (priced higher on some models).
    long_input_tokens: int = 0
    long_output_tokens: int = 0
    long_cache_read_tokens: int = 0
    long_cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens
                + self.long_input_tokens + self.long_output_tokens + self.long_cache_read_tokens + self.long_cache_creation_tokens)

    @property
    def prompt_tokens(self) -> int:
        """The prompt of a single request: all its input tokens, cache reads and writes included."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def as_long(self) -> "Usage":
        """The same request's tokens, moved to the long-prompt buckets."""
        return Usage(web_searches=self.web_searches, long_input_tokens=self.input_tokens, long_output_tokens=self.output_tokens,
                     long_cache_read_tokens=self.cache_read_tokens, long_cache_creation_tokens=self.cache_creation_tokens)

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.web_searches += other.web_searches
        self.long_input_tokens += other.long_input_tokens
        self.long_output_tokens += other.long_output_tokens
        self.long_cache_read_tokens += other.long_cache_read_tokens
        self.long_cache_creation_tokens += other.long_cache_creation_tokens
