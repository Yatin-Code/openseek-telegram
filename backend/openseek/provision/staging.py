"""Compose the temporary staging configuration on the chosen Supabase project.

One private Storage bucket holds upload staging only until Telegram accepts a
package: Telegram stays the canonical store (docs/adr/0001), so the bucket is
temporary by design and its per-file cap matches the NVMT package ceiling.
"""
from __future__ import annotations

from ..pool import MAX_NVMT_BYTES
from ..remote import SupabaseClient

# The single name the OpenSeek docs (docs/VERCEL.md) and config's default bucket use.
DEFAULT_STAGING_BUCKET = "openseek-staging"


def project_url(ref: str) -> str:
    """The participant project's data-plane origin, derived from its ref alone.

    Only the project's own host exists here: no credentials, no query, no fragment
    (Settings.validate requires an HTTPS origin without those).
    """
    return f"https://{ref}.supabase.co"


def ensure_staging_bucket(provisioner, *, ref: str, service_key: str,
                          bucket: str = DEFAULT_STAGING_BUCKET,
                          size_limit: int = MAX_NVMT_BYTES,
                          transport=None) -> str:
    """Create (or confirm) the private staging bucket and describe what happened.

    Listing goes through the management API; creation uses the project's own Storage
    REST surface with the service key because that is where bucket creation lives.
    Returns a plain detail string for the wizard's summary ("created" / "already
    existed" / the bucket name and cap). Any mismatch in shape is a typed error, never
    a secret leak.
    """
    names = {str(row.get("name") or row.get("id") or "")
             for row in provisioner.list_buckets(ref)}
    if bucket in names:
        created = False
    else:
        client = SupabaseClient(project_url(ref), service_key, transport=transport,
                                bucket=bucket)
        try:
            created = client.create_bucket(bucket, public=False, size_limit=size_limit)
        finally:
            client.close()
    state = ("created" if created else "already existed (kept)")
    return f"Private staging bucket {bucket} {state} ({size_limit} bytes per file)."
