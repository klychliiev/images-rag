from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    google_cloud_scopes: str
    pinecone_api_key: str
    openai_api_key: str
    default_index_name: str


settings = Settings()
