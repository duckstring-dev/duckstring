-- App instances on other machines (plans/app-scaling.md): where an instance ran.
ALTER TABLE app_instance ADD COLUMN place TEXT NOT NULL DEFAULT 'catchment';
