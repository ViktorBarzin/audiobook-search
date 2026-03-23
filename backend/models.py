from pydantic import BaseModel


class AudiobookResult(BaseModel):
    id: str
    title: str
    author: str | None = None
    narrator: str | None = None
    format: str | None = None
    size: str | None = None
    url: str
    cover_url: str | None = None


class AudiobookDetail(AudiobookResult):
    magnet_url: str
    description: str | None = None
    language: str | None = None
