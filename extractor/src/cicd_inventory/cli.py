"""Command-line entry point for the CI/CD inventory extractor."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
import httpx

from cicd_inventory.providers.github import GitHubProvider
from cicd_inventory.writer import write_collections

log = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _preflight_check(token: str) -> None:
    """Verify the token has the necessary scopes by hitting the REST meta endpoint."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.github.com/",
            headers={"Authorization": f"bearer {token}"},
        )
        if resp.status_code != 200:
            click.echo(f"Token check failed (HTTP {resp.status_code}). Is GITHUB_TOKEN valid?", err=True)
            sys.exit(1)

        scopes = resp.headers.get("X-OAuth-Scopes", "")
        log.debug("Token scopes: %s", scopes or "(none — may be a fine-grained PAT)")
        # Fine-grained PATs don't expose scopes; skip the check for them.
        if scopes and "repo" not in scopes and "public_repo" not in scopes:
            click.echo(
                f"Warning: token scopes ({scopes!r}) may not include repo read access.",
                err=True,
            )


@click.command()
@click.option(
    "--orgs",
    default="BioContainers,galaxyproject,usegalaxy-eu",
    show_default=True,
    help="Comma-separated list of GitHub organization names to scan.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=Path("../site/src/content/workflows"),
    show_default=True,
    help="Directory where per-workflow JSON files will be written.",
)
@click.option(
    "--token",
    envvar="GITHUB_TOKEN",
    required=True,
    help="GitHub personal access token (read:org + public_repo scopes).",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.option(
    "--skip-preflight",
    is_flag=True,
    help="Skip the token scope preflight check.",
)
def main(
    orgs: str,
    output_dir: Path,
    token: str,
    verbose: bool,
    skip_preflight: bool,
) -> None:
    """Fetch GitHub Actions workflows from one or more organizations and write them as JSON."""
    _configure_logging(verbose)

    org_list = [o.strip() for o in orgs.split(",") if o.strip()]
    if not org_list:
        click.echo("No organizations specified.", err=True)
        sys.exit(1)

    async def _run() -> None:
        if not skip_preflight:
            await _preflight_check(token)

        provider = GitHubProvider(token=token)
        records = await provider.fetch_workflows(org_list)

        if not records:
            log.warning("No workflow records found — check org names and token permissions.")

        parse_errors = provider.parse_errors
        if parse_errors:
            log.warning(
                "%d workflow file(s) skipped due to parse errors (listed in _index.json)",
                len(parse_errors),
            )

        write_collections(records, output_dir.resolve(), parse_errors=parse_errors)
        click.echo(f"Done. {len(records)} workflows written to {output_dir}.")

    asyncio.run(_run())
