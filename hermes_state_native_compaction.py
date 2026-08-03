"""Native OpenAI compaction checkpoint persistence for SessionDB."""

from __future__ import annotations

import json

from agent.native_openai_compaction import (
    NativeCompactionCheckpoint,
    NativeCompactionIdentity,
)


class NativeOpenAICompactionStateMixin:
    """Persist opaque native compaction state outside visible transcripts."""

    def upsert_native_openai_checkpoint(
        self, checkpoint: NativeCompactionCheckpoint
    ) -> None:
        """Atomically replace the checkpoint row for its session."""
        identity = checkpoint.identity
        values = (
            checkpoint.session_id,
            identity.provider,
            identity.api_mode,
            identity.model,
            identity.base_url,
            identity.issuer_kind,
            identity.credential_scope,
            int(identity.replay_encrypted_reasoning),
            checkpoint.source_input_item_count,
            checkpoint.source_input_sha256,
            checkpoint.output_json,
            checkpoint.compact_response_id,
            checkpoint.compact_created_at,
            checkpoint.input_item_count,
            checkpoint.output_item_count,
            checkpoint.generation,
            checkpoint.created_at,
            checkpoint.updated_at,
        )

        def _do(conn):
            conn.execute(
                """INSERT INTO native_openai_compaction_checkpoints (
                       session_id, provider, api_mode, model, base_url,
                       issuer_kind, credential_scope, replay_encrypted_reasoning,
                       source_input_item_count, source_input_sha256, output_json,
                       compact_response_id, compact_created_at, input_item_count,
                       output_item_count, generation, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       provider = excluded.provider,
                       api_mode = excluded.api_mode,
                       model = excluded.model,
                       base_url = excluded.base_url,
                       issuer_kind = excluded.issuer_kind,
                       credential_scope = excluded.credential_scope,
                       replay_encrypted_reasoning = excluded.replay_encrypted_reasoning,
                       source_input_item_count = excluded.source_input_item_count,
                       source_input_sha256 = excluded.source_input_sha256,
                       output_json = excluded.output_json,
                       compact_response_id = excluded.compact_response_id,
                       compact_created_at = excluded.compact_created_at,
                       input_item_count = excluded.input_item_count,
                       output_item_count = excluded.output_item_count,
                       generation = excluded.generation,
                       created_at = excluded.created_at,
                       updated_at = excluded.updated_at""",
                values,
            )

        self._execute_write(_do)

    def load_native_openai_checkpoint(
        self, session_id: str
    ) -> NativeCompactionCheckpoint | None:
        """Load and validate a checkpoint, failing open on malformed state."""
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT * FROM native_openai_compaction_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None

        try:
            text_fields = (
                "session_id",
                "provider",
                "api_mode",
                "model",
                "base_url",
                "issuer_kind",
                "credential_scope",
                "source_input_sha256",
                "output_json",
            )
            if any(type(row[field]) is not str for field in text_fields):
                return None
            compact_response_id = row["compact_response_id"]
            if compact_response_id is not None and type(compact_response_id) is not str:
                return None
            integer_fields = (
                "source_input_item_count",
                "input_item_count",
                "output_item_count",
                "generation",
            )
            if any(type(row[field]) is not int for field in integer_fields):
                return None
            replay_encrypted_reasoning = row["replay_encrypted_reasoning"]
            if type(replay_encrypted_reasoning) is not int or replay_encrypted_reasoning not in (
                0,
                1,
            ):
                return None
            for field in ("created_at", "updated_at"):
                if type(row[field]) not in (int, float):
                    return None
            compact_created_at = row["compact_created_at"]
            if compact_created_at is not None and type(compact_created_at) not in (
                int,
                float,
            ):
                return None

            output = json.loads(row["output_json"])
            identity = NativeCompactionIdentity(
                provider=row["provider"],
                api_mode=row["api_mode"],
                model=row["model"],
                base_url=row["base_url"],
                issuer_kind=row["issuer_kind"],
                credential_scope=row["credential_scope"],
                replay_encrypted_reasoning=bool(replay_encrypted_reasoning),
            )
            return NativeCompactionCheckpoint(
                session_id=row["session_id"],
                identity=identity,
                source_input_item_count=row["source_input_item_count"],
                source_input_sha256=row["source_input_sha256"],
                output=output,
                compact_response_id=compact_response_id,
                compact_created_at=compact_created_at,
                input_item_count=row["input_item_count"],
                output_item_count=row["output_item_count"],
                generation=row["generation"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception:
            # Persistence is an optimization. Never surface or log opaque output,
            # credentials, or parser errors containing either value.
            return None

    def delete_native_openai_checkpoint(self, session_id: str) -> bool:
        """Delete one session checkpoint, returning whether a row existed."""

        def _do(conn):
            cursor = conn.execute(
                "DELETE FROM native_openai_compaction_checkpoints WHERE session_id = ?",
                (session_id,),
            )
            return cursor.rowcount > 0

        return bool(self._execute_write(_do))
