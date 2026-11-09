from __future__ import annotations

import pytest

from openseek.provision.errors import CONFLICT, ProviderError
from openseek.provision.staging import DEFAULT_STAGING_BUCKET, ensure_staging_bucket


class BucketListing:
    def __init__(self, row):
        self.row = row

    def list_buckets(self, _ref):
        return [self.row]


def test_existing_staging_bucket_settings_are_reported_from_provider():
    provisioner = BucketListing({"name": DEFAULT_STAGING_BUCKET, "public": False,
                                 "file_size_limit": 16 * 1024 * 1024})
    detail = ensure_staging_bucket(provisioner, ref="synthetic1", service_key="synthetic-service-key")
    assert "already existed" in detail
    assert "16777216 bytes per file" in detail


@pytest.mark.parametrize("settings", [
    {"public": True, "file_size_limit": 16 * 1024 * 1024},
    {"public": False, "file_size_limit": 1024},
    {"public": False},
])
def test_mismatched_existing_staging_bucket_stops_setup(settings):
    provisioner = BucketListing({"name": DEFAULT_STAGING_BUCKET, **settings})
    with pytest.raises(ProviderError) as caught:
        ensure_staging_bucket(provisioner, ref="synthetic1", service_key="synthetic-service-key")
    assert caught.value.code == CONFLICT
    assert "existing staging bucket" in caught.value.plain.lower()
