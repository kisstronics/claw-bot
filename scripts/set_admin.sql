-- Promote a user to admin by email (run against your Postgres DATABASE_URL).
-- Example:
--   psql "$DATABASE_URL" -f scripts/set_admin.sql
-- Or edit the email below, then run.

SET search_path TO claw, public;

UPDATE users
SET role = 'admin', updated_at = now()
WHERE lower(email) = lower('chvkishore.20@gmail.com');

SELECT user_id, email, name, role FROM users WHERE role = 'admin';
