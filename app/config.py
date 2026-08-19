from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Environment-driven configuration. Values are set as Application Settings in the
    Azure App Service Configuration blade in the cloud, and via a local .env file
    (see .env.example) when running locally. Never commit real values.
    """

    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment_name: str = "gpt-5-mini"
    azure_openai_api_version: str = "2024-12-01-preview"

    azure_storage_connection_string: str = ""

    # Application Insights (Tier 1, structured logging/observability — see app/main.py's
    # configure_azure_monitor() call). Empty string means "not configured," same convention as
    # every other Azure value above — main.py treats that as "skip App Insights setup," not an error.
    applicationinsights_connection_string: str = ""

    # Pass/fail cutoff for POST /evaluate's overall_score (mean of groundedness/relevance/coherence,
    # each 0-1). 0.75 default per decision-log.md §13 — env-overridable, not hardcoded in generation.py,
    # so it can be tuned post-launch without a code change.
    evaluation_pass_threshold: float = 0.75

    class Config:
        env_file = ".env"


settings = Settings()
