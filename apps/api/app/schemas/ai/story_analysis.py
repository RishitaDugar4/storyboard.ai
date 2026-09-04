"""Stage 1 contract: a structured *reading* of the story.

Cheap, re-runnable, and deliberately separate from the storyboard. Analysis
answers "what is in this text"; the storyboard answers "how should it be
filmed". Keeping them apart means you can re-read a revised story without
discarding a storyboard you have already curated.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"

Role = Literal["protagonist", "antagonist", "supporting", "incidental"]
Valence = Literal["setup", "rising", "turn", "climax", "resolution"]


class DetectedCharacter(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=6)
    role: Role
    #: Evidence from the text. Requiring this discourages invention: a
    #: character the model cannot quote is a character it made up.
    description_from_text: str = Field(min_length=1, max_length=600)
    first_mention_excerpt: str = Field(min_length=1, max_length=300)


class DetectedLocation(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description_from_text: str = Field(min_length=1, max_length=600)
    interior: bool = False


class Beat(BaseModel):
    index: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=300)
    source_excerpt: str = Field(min_length=1, max_length=400)
    emotional_valence: Valence


class StoryAnalysis(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    title: str = Field(min_length=1, max_length=120)
    logline: str = Field(min_length=1, max_length=240)
    tone: list[str] = Field(min_length=1, max_length=5)
    setting_summary: str = Field(min_length=1, max_length=600)
    characters: list[DetectedCharacter] = Field(min_length=1, max_length=12)
    locations: list[DetectedLocation] = Field(default_factory=list, max_length=12)
    beats: list[Beat] = Field(min_length=3, max_length=40)

    @model_validator(mode="after")
    def _beats_are_ordered_and_complete(self):
        indices = [b.index for b in self.beats]
        if indices != sorted(indices):
            raise ValueError("beats must be listed in narrative order")
        if len(set(indices)) != len(indices):
            raise ValueError("beat indices must be unique")
        return self

    @model_validator(mode="after")
    def _exactly_one_protagonist_ish(self):
        leads = [c.name for c in self.characters if c.role == "protagonist"]
        if not leads:
            raise ValueError(
                "no protagonist identified; mark the character the story "
                "follows as 'protagonist'")
        if len(leads) > 2:
            raise ValueError(
                f"{len(leads)} protagonists ({', '.join(leads)}); at most two. "
                "Demote the rest to 'supporting'.")
        return self
