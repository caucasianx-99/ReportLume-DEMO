"""Immutable report revisions, integrity checks and trial-safe publishing.

Publishing updates summary metadata without rewriting frozen revision
documents.

V1 snapshots remain readable; new revision snapshots use schema V2.
"""

import copy
import hashlib
import json
import re
import uuid

from datetime import datetime, timezone

from app.core.firestore_rest import (
    FirestoreNotFound,
    FirestoreConflict,
    FirestoreUnavailable,
    encode_fields,
    decode_fields,
)

from app.services.client_workspace import (
    WorkspaceReadOnly,
    has_paid_access,
    require_write_access,
    make_update,
)

from app.services.proof_of_work import month_bounds
from app.services.monthly_defaults import is_unfinished_month
from app.services.reporting import ReportingService
from app.services.workspace_access import (
    can_publish_unmarked_copy,
    plan_entitlements,
)
from app.core.config import settings


REVISION = re.compile('^[a-f0-9]{32}$')
MAX_SNAPSHOT_BYTES = 600000


class ReportValidation(ValueError):
    pass


class ReportConflict(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def create(name, values):
    return {
        'update': {
            'name': name,
            'fields': encode_fields(values),
        },
        'currentDocument': {
            'exists': False,
        },
    }


def check_notes(payload):
    if not isinstance(payload, dict):
        raise ReportValidation('Invalid report form.')

    limits = {
        'brand': 80,
        'executive_summary': 2000,
        'next_month_plan': 1500,
        'client_message': 1500,
    }

    output = {}

    for key, limit in limits.items():
        value = payload.get(key, '')

        if not isinstance(value, str) or len(value) > limit:
            raise ReportValidation(
                f'{key} must be at most {limit} characters.'
            )

        if any(
            ord(character) < 32 and character not in '\n\t'
            for character in value
        ):
            raise ReportValidation(
                f'{key} contains forbidden control characters.'
            )

        output[key] = value.strip()

    output['brand'] = output['brand'] or 'Your Agency'
    return output


def _public_summary(snapshot):
    observed = snapshot.get('observed', {})
    impressions = observed.get('impressions', {})
    engagements = observed.get('engagements', {})

    result = {
        'sample_size': impressions.get('n'),
        'median_impressions': impressions.get('median'),
        'median_engagements': engagements.get('median'),
        'quality': snapshot.get('quality', {}),
    }

    if snapshot.get('schema_version') == 2:
        result.update({
            'schema_version': 2,
            'post_count': observed.get('post_count'),
            'impressions_coverage': impressions.get('coverage'),
            'engagements_coverage': engagements.get('coverage'),
        })

    return result


def snapshot_branding(profile):
    """Freeze allowed branding into a new snapshot without erasing settings.

    A Solo user can retain their saved preferences for a later upgrade, but
    new Solo previews and revisions always use the standard report design.
    Previously published frozen revisions are never rewritten.
    """
    rights = plan_entitlements(profile, settings)

    if rights["allow_custom_branding"]:
        return {
            "brand_theme": profile.get("brand_theme") or "teal",
            "brand_logo_b64": profile.get("brand_logo_b64") or "",
        }

    return {
        "brand_theme": "teal",
        "brand_logo_b64": "",
    }


class ReportStore:

    def __init__(self, store):
        self.store = store

    def _name(self, client_id, period):
        month_bounds(period)
        return self.store.client_name(client_id) + '/reports/' + period

    def _revision_name(self, client_id, period, revision):
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise ReportValidation('Invalid report revision.')

        return self._name(client_id, period) + '/revisions/' + revision

    def _can_write(self, client_id):
        profile, version = self.store.get_user_with_version()
        paid = require_write_access(profile)
        client = self.store.get_client(client_id)

        if client.get('status') != 'active':
            raise WorkspaceReadOnly(
                'Only active clients allow report changes.'
            )

        if not paid and profile.get('unpaid_client_id') != client_id:
            raise WorkspaceReadOnly(
                'Client is outside trial allowance.'
            )

        return (profile, version, paid)

    def get_parent(self, client_id, period):
        name = self._name(client_id, period)

        try:
            doc = self.store.get_document(name)
        except FirestoreNotFound:
            return ({}, '')

        return (
            decode_fields(doc.get('fields', {})),
            doc['updateTime'],
        )

    def get_revision(self, client_id, period, revision):
        self.store.get_client(client_id)

        doc = self.store.get_document(
            self._revision_name(client_id, period, revision)
        )

        fields = decode_fields(doc.get('fields', {}))
        raw = fields.get('snapshot_json')

        if (
            not isinstance(raw, str)
            or len(raw.encode('utf-8')) > MAX_SNAPSHOT_BYTES
        ):
            raise FirestoreUnavailable(
                'Stored report snapshot is invalid.'
            )

        try:
            snapshot = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise FirestoreUnavailable(
                'Stored report snapshot is invalid.'
            ) from exc

        expected = hashlib.sha256(
            raw.encode('utf-8')
        ).hexdigest()

        if (
            expected != fields.get('sha256')
            or not isinstance(snapshot, dict)
        ):
            raise FirestoreUnavailable(
                'Report snapshot integrity check failed.'
            )

        if snapshot.get('schema_version', 1) not in (1, 2):
            raise FirestoreUnavailable(
                'Unsupported report snapshot version.'
            )

        if snapshot.get('period') != period:
            raise FirestoreUnavailable(
                'Report period integrity check failed.'
            )

        return (fields, snapshot)

    def create_revision(self, client_id, period, raw_notes):
        self._can_write(client_id)

        notes = check_notes(raw_notes)

        snapshot = ReportingService(self.store).build(
            client_id, period, notes
        )

        profile, _ = self.store.get_user_with_version()

        snapshot['trial_watermark'] = not can_publish_unmarked_copy(
            profile, settings
        )
        snapshot.update(snapshot_branding(profile))

        raw = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )

        if len(raw.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
            raise ReportValidation(
                'Report exceeds the supported snapshot size.'
            )

        revision = uuid.uuid4().hex
        parent_name = self._name(client_id, period)
        _, version = self.get_parent(client_id, period)
        stamp = now()

        writes = [
            create(
                self._revision_name(
                    client_id,
                    period,
                    revision,
                ),
                {
                    'owner_uid': self.store.uid,
                    'client_id': client_id,
                    'period': period,
                    'revision_id': revision,
                    'sha256': hashlib.sha256(
                        raw.encode('utf-8')
                    ).hexdigest(),
                    'snapshot_json': raw,
                    'created_at': stamp,
                    'source': 'x_analytics + ghostwriter_input + computed',
                    'schema_version': snapshot.get(
                        'schema_version',
                        1,
                    ),
                },
            )
        ]

        values = {
            'period': period,
            'client_id': client_id,
            'owner_uid': self.store.uid,
            'latest_revision': revision,
            'updated_at': stamp,
        }

        if version:
            writes.append(
                make_update(
                    parent_name,
                    values,
                    version,
                )
            )
        else:
            writes.append(
                create(
                    parent_name,
                    {
                        **values,
                        'published_revision': None,
                        'status': 'draft',
                    },
                )
            )

        try:
            self.store.commit(writes)
        except FirestoreConflict as exc:
            raise ReportConflict(
                'Report changed. Refresh and retry.'
            ) from exc

        return revision

    def publish(
        self,
        client_id,
        period,
        revision,
        *,
        confirm_incomplete_month=False,
        confirm_trial_use=False,
    ):
        profile, profile_version, paid = self._can_write(client_id)
        _, snapshot = self.get_revision(client_id, period, revision)
        parent, version = self.get_parent(client_id, period)

        if not version:
            raise FirestoreNotFound('Report not found')

        if revision not in (
            parent.get('latest_revision'),
            parent.get('published_revision'),
        ):
            raise ReportValidation(
                'Choose a current report revision.'
            )

        observed = snapshot.get('observed') or {}
        post_count = (
            observed.get('post_count')
            if isinstance(observed, dict)
            else None
        )

        if type(post_count) is not int or post_count <= 0:
            raise ReportValidation(
                'A report needs completed-import posts before publishing.'
            )

        brand = snapshot.get('brand')

        if (
            not isinstance(brand, str)
            or not brand.strip()
            or brand.strip().casefold() == 'your agency'
        ):
            raise ReportValidation(
                'Set your agency name before publishing.'
            )

        if is_unfinished_month(period) and not confirm_incomplete_month:
            raise ReportValidation(
                'Confirm that you want to publish an unfinished month.'
            )

        if (
            snapshot.get('trial_watermark') is True
            and can_publish_unmarked_copy(profile, settings)
        ):
            return self._publish_unmarked_source(
                client_id,
                period,
                revision,
                parent,
                version,
            )

        if not paid:
            if not profile.get('trial_started_at'):
                raise WorkspaceReadOnly(
                    'Trial starts after the first confirmed real CSV import.'
                )

            prior_client = profile.get('trial_report_client_id')
            prior_period = profile.get('trial_report_period')

            if not prior_client and not confirm_trial_use:
                raise ReportValidation(
                    'Confirm which month will use your one trial report.'
                )

            if prior_client is not None and (
                prior_client != client_id or prior_period != period
            ):
                raise WorkspaceReadOnly(
                    'The one trial report has already been used.'
                )

        if (
            paid
            and snapshot.get('trial_watermark') is False
            and not can_publish_unmarked_copy(profile, settings)
        ):
            raise WorkspaceReadOnly(
                'Verified paid access is required to publish an unmarked report.'
            )

        stamp = now()

        summary = json.dumps(
            _public_summary(snapshot),
            ensure_ascii=False,
            separators=(',', ':'),
        )

        writes = [
            make_update(
                self._name(client_id, period),
                {
                    'published_revision': revision,
                    'published_summary_json': summary,
                    'published_at': stamp,
                    'updated_at': stamp,
                    'status': 'published',
                },
                version,
            )
        ]

        if not paid and not profile.get('trial_report_client_id'):
            writes.append(
                make_update(
                    self.store.user_name,
                    {
                        'trial_report_client_id': client_id,
                        'trial_report_period': period,
                    },
                    profile_version,
                )
            )

        client = self.store.get_client(client_id)
        last_period = client.get('last_published_period')

        if not isinstance(last_period, str) or period > last_period:
            client_version = client.get('_update_time')

            if client_version:
                writes.append(
                    make_update(
                        self.store.client_name(client_id),
                        {
                            'last_published_period': period,
                            'last_published_at': stamp,
                        },
                        client_version,
                    )
                )

        try:
            self.store.commit(writes)
        except FirestoreConflict as exc:
            raise ReportConflict(
                'Publishing conflicted with another update.'
            ) from exc

        return revision

    def _publish_unmarked_source(
        self,
        client_id,
        period,
        source_id,
        parent,
        version,
    ):
        """Copy immutable content and atomically update report pointers.

        When the source is the latest saved revision, the new unmarked
        version becomes both latest and published. When a newer saved
        draft exists, only published advances; that draft stays intact.
        """
        if not version or source_id not in {
            parent.get('published_revision'),
            parent.get('latest_revision'),
        }:
            raise ReportValidation(
                'Choose the latest saved or published revision.'
            )

        _, source = self.get_revision(
            client_id,
            period,
            source_id,
        )

        if source.get('trial_watermark') is not True:
            raise ReportValidation(
                'The source has no trial mark.'
            )

        cloned = copy.deepcopy(source)
        cloned['trial_watermark'] = False

        raw = json.dumps(
            cloned,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )

        if len(raw.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
            raise ReportValidation(
                'The copied report is too large.'
            )

        revision_id = uuid.uuid4().hex
        timestamp = now()
        checksum = hashlib.sha256(
            raw.encode('utf-8')
        ).hexdigest()

        # The source's identity determines whether the saved pointer
        # can advance without discarding a different pending draft.
        parent_update = {
            'published_revision': revision_id,
            'published_summary_json': json.dumps(
                _public_summary(cloned),
                ensure_ascii=False,
                separators=(',', ':'),
            ),
            'published_at': timestamp,
            'updated_at': timestamp,
            'status': 'published',
        }

        if parent.get('latest_revision') == source_id:
            parent_update['latest_revision'] = revision_id

        writes = [
            create(
                self._revision_name(
                    client_id,
                    period,
                    revision_id,
                ),
                {
                    'owner_uid': self.store.uid,
                    'client_id': client_id,
                    'period': period,
                    'revision_id': revision_id,
                    'sha256': checksum,
                    'snapshot_json': raw,
                    'created_at': timestamp,
                    'source': 'frozen_trial_revision_copy',
                    'copied_from_revision_id': source_id,
                    'copied_at': timestamp,
                    'schema_version': cloned.get(
                        'schema_version',
                        1,
                    ),
                },
            ),
            make_update(
                self._name(client_id, period),
                parent_update,
                version,
            ),
        ]

        client = self.store.get_client(client_id)

        if (
            not isinstance(client.get('last_published_period'), str)
            or period > client['last_published_period']
        ):
            version_of_client = client.get('_update_time')

            if version_of_client:
                writes.append(
                    make_update(
                        self.store.client_name(client_id),
                        {
                            'last_published_period': period,
                            'last_published_at': timestamp,
                        },
                        version_of_client,
                    )
                )

        try:
            self.store.commit(writes)
        except FirestoreConflict as exc:
            raise ReportConflict(
                'Report changed. Refresh and retry.'
            ) from exc

        return revision_id

    def publish_paid_copy(
        self,
        client_id,
        period,
        source_revision=None,
    ):
        """Publish an unmarked immutable copy without changing saved drafts."""
        profile, _, _ = self._can_write(client_id)

        if not can_publish_unmarked_copy(profile, settings):
            raise WorkspaceReadOnly(
                'Verified paid access is required for an unmarked copy.'
            )

        parent, version = self.get_parent(client_id, period)

        if not version:
            raise ReportValidation(
                'Save or publish a report before copying it.'
            )

        allowed = {
            parent.get('latest_revision'),
            parent.get('published_revision'),
        }

        if source_revision and source_revision not in allowed:
            raise ReportValidation(
                'Choose the latest saved or published revision.'
            )

        # A copy is only valid from a current pointer.
        source_id = source_revision

        if source_id is None:
            for candidate in (
                parent.get('published_revision'),
                parent.get('latest_revision'),
            ):
                if not candidate:
                    continue

                _, candidate_snapshot = self.get_revision(
                    client_id,
                    period,
                    candidate,
                )

                if candidate_snapshot.get('trial_watermark') is True:
                    source_id = candidate
                    break

        if not source_id:
            raise ReportValidation(
                'No eligible trial-marked revision found.'
            )

        if source_id != parent.get('published_revision'):
            _, candidate_snapshot = self.get_revision(
                client_id,
                period,
                source_id,
            )

            observed = candidate_snapshot.get('observed') or {}
            brand = candidate_snapshot.get('brand')
            count = (
                observed.get('post_count')
                if isinstance(observed, dict)
                else None
            )

            if type(count) is not int or count <= 0:
                raise ReportValidation(
                    'A report needs completed-import posts before publishing.'
                )

            if (
                not isinstance(brand, str)
                or not brand.strip()
                or brand.strip().casefold() == 'your agency'
            ):
                raise ReportValidation(
                    'Set your agency name before publishing.'
                )

            if is_unfinished_month(period):
                raise ReportValidation(
                    'Confirm unfinished-month publishing through the normal Publish action.'
                )

        return self._publish_unmarked_source(
            client_id,
            period,
            source_id,
            parent,
            version,
        )

    def list_revisions(self, client_id, period):
        """Complete version history so labels are stable beyond 50 records."""
        self.store.get_client(client_id)

        path = self._name(client_id, period) + '/revisions'
        token = ''
        visited = set()
        items = []

        while True:
            response = self.store.list_collection(
                path,
                page_size=50,
                page_token=token,
            )

            for document in response.get('documents', []):
                fields = decode_fields(
                    document.get('fields', {})
                )

                items.append({
                    'id': document['name'].rsplit('/', 1)[-1],
                    'created_at': fields.get('created_at', ''),
                    'copied_from_revision_id': fields.get(
                        'copied_from_revision_id',
                        '',
                    ),
                })

            next_token = response.get('nextPageToken', '')

            if not next_token:
                break

            if next_token in visited or len(items) > 10000:
                raise FirestoreUnavailable(
                    'Could not page report version history.'
                )

            visited.add(next_token)
            token = next_token

        ordered = sorted(
            items,
            key=lambda row: (
                row['created_at'],
                row['id'],
            ),
        )

        for index, row in enumerate(ordered, start=1):
            row['version_number'] = index

        return (
            sorted(
                ordered,
                key=lambda row: row['version_number'],
                reverse=True,
            ),
            False,
        )

    def published(self, client_id, period):
        parent, _ = self.get_parent(client_id, period)
        revision = parent.get('published_revision')

        if not revision:
            raise FirestoreNotFound('No published report')

        return self.get_revision(
            client_id,
            period,
            revision,
        )


class ReportPreviewService:
    """Create an unsaved report preview through the normal report builder."""

    def __init__(self, store):
        self.store = store

    def build(
        self,
        client_id: str,
        period: str,
        raw_notes: dict,
    ) -> dict:
        """Return an ephemeral owner-only report; never commit it."""

        profile, _, paid = ReportStore(
            self.store
        )._can_write(client_id)

        notes = check_notes(raw_notes)

        snapshot = ReportingService(
            self.store
        ).build(
            client_id,
            period,
            notes,
        )

        # A preview does not consume the trial report allowance.
        snapshot["trial_watermark"] = not paid

        snapshot.update(snapshot_branding(profile))

        raw = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ReportValidation(
                "Report preview exceeds the supported snapshot size."
            )

        return snapshot
