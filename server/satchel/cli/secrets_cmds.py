"""``satchel secrets``: the key that encrypts secrets stored in Postgres (satchel.core.crypto)."""

from __future__ import annotations

from typing import Annotated

import typer


def register_secrets_commands(app: typer.Typer) -> None:
    secrets_app = typer.Typer(help="Encryption of secrets stored in the database",
                              no_args_is_help=True)
    app.add_typer(secrets_app, name="secrets")

    @secrets_app.command("generate-key")
    def generate_key() -> None:
        """Print a new SATCHEL_ENCRYPTION_KEY (256 bits, hex). Same as `openssl rand -hex 32`."""
        from satchel.core.crypto import generate_key as gen

        typer.echo(gen())

    @secrets_app.command("rotate")
    def rotate(
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Count only")] = False,
    ) -> None:
        """Re-encrypt every stored secret with SATCHEL_ENCRYPTION_KEY.

        Rotation: set the new key as SATCHEL_ENCRYPTION_KEY and the old one in
        SATCHEL_ENCRYPTION_KEY_PREVIOUS, deploy, run this, then remove the old key."""
        from satchel.core.crypto import EncryptionUnavailable
        from satchel.secrets import rotate_secrets
        from satchel.settings import get_settings

        try:
            rep = rotate_secrets(get_settings(), dry_run=dry_run)
        except EncryptionUnavailable as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
        verb = "would re-encrypt" if dry_run else "re-encrypted"
        typer.echo(f"{verb} {rep.rotated}; already on the current key: {rep.current}")
        for f in rep.failed:
            typer.echo(f"failed: {f}", err=True)
        if rep.failed:
            raise typer.Exit(1)
