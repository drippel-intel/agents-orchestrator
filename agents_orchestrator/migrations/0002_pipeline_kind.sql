-- Add pipeline kind so BI and generic software-dev pipelines can share the state machine.

ALTER TABLE pipeline ADD COLUMN kind TEXT NOT NULL DEFAULT 'bi';
