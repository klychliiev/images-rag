from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    google_cloud_scopes: str

    openai_api_key: str

    pinecone_index_default: str
    pinecone_api_key: str
    pinecone_cloud: str
    pinecone_region: str

    aws_region: str
    aws_account_id: str
    registry_name: str
    tag: str


settings = Settings()
