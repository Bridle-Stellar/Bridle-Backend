"""
Central runtime configuration for Bridle Backend.

Everything that differs between a dev laptop, a testnet deployment, and a
future mainnet deployment lives here, sourced from environment variables
(see .env.example). Nothing in this module should ever hold a literal
secret or network-specific value — that defeats the point.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Stellar / Soroban -------------------------------------------------
    stellar_network: Literal["testnet", "futurenet", "mainnet"] = Field(
        default="testnet",
        description="Which Stellar network this deployment talks to.",
    )
    soroban_rpc_url: str = Field(
        default="https://soroban-testnet.stellar.org",
        description="Soroban RPC endpoint used for all contract reads, simulation, and submission.",
    )
    network_passphrase: str = Field(
        default="Test SDF Network ; September 2015",
        description="Network passphrase matching stellar_network — must match soroban_rpc_url.",
    )
    bridle_contract_id: str = Field(
        default="",
        description="Contract ID of the deployed Bridle Contract instance this backend guards.",
    )

    # --- Relayer signing key -------------------------------------------------
    # AUTH MODEL (confirmed against Bridle Contract's README — see this repo's
    # README "Auth model" section for the full explanation):
    #   check_and_record_spend and the SEP-41 transfer that follows it both
    #   require the *agent's* Soroban authorization (agent.require_auth()),
    #   not the relayer's. This key never authorizes a spend and is never the
    #   "from" of a transfer — it exists purely to submit transactions and
    #   pay their network fee as the source account, using authorization
    #   entries the agent has already signed with its own key (see
    #   app/services/soroban_client.py). A compromised relayer key can waste
    #   this account's XLM on fees; it cannot move funds or approve spends on
    #   its own.
    relayer_secret_key: str = Field(
        default="",
        description="Secret seed (S...) for the relayer's fee-paying Stellar account. Never commit a real value.",
    )

    # --- Database -------------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./bridle.db",
        description=(
            "SQLAlchemy async database URL. Defaults to a local SQLite file for zero-dependency "
            "setup. Swap to e.g. postgresql+asyncpg://user:pass@host/db for production — the data "
            "layer (app/database.py, app/models.py) is written against SQLAlchemy's async ORM and "
            "does not assume SQLite-specific behavior."
        ),
    )

    # --- Policy pre-check cache -------------------------------------------------
    policy_cache_ttl_seconds: float = Field(
        default=2.0,
        description=(
            "How long a fetched policy snapshot (allowlist, caps, kill switch, current spend) is "
            "reused before re-reading it from the chain. This is a latency optimization for the "
            "local pre-check only — the on-chain check_and_record_spend call is always the final "
            "authority and is never skipped or cached."
        ),
    )

    auth_entry_validity_ledgers: int = Field(
        default=60,
        description=(
            "How many ledgers past the current one a prepared authorization entry stays valid "
            "for (~5 minutes at Stellar's ~5s ledger close time). This is the window an agent has "
            "to sign and return an entry from /relay/prepare before it expires; the network itself "
            "enforces the expiration at submission, so this is a UX/latency budget, not a security "
            "control."
        ),
    )

    # --- Sync worker -------------------------------------------------------------
    sync_poll_interval_seconds: float = Field(
        default=5.0,
        description=(
            "How often the background worker polls the contract for new events. Lower values "
            "shrink the window where an out-of-band spend (e.g. manual CLI testing) is missing "
            "from the local log; higher values reduce RPC load. 5s is a reasonable default for "
            "testnet development."
        ),
    )
    sync_worker_enabled: bool = Field(
        default=True,
        description="Set to false to disable the background event-sync worker (e.g. in tests).",
    )

    # --- HTTP service -------------------------------------------------------------
    cors_allow_origins: str = Field(
        default="*",
        description="Comma-separated list of origins allowed to call this API (e.g. the dashboard's URL).",
    )
    request_timeout_seconds: float = Field(
        default=8.0,
        description=(
            "Overall budget for a single /relay call, including RPC round trips. This sits in a "
            "payment hot path: a typical Soroban RPC round trip on testnet is 200-800ms, and a "
            "relay call makes at most two of them (policy read + check_and_record_spend), so a "
            "healthy call should complete well under this budget. Treat repeated near-timeout "
            "calls as a signal to investigate RPC latency, not to raise this number."
        ),
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Settings are cached for process lifetime; tests override via dependency_overrides."""
    return Settings()
