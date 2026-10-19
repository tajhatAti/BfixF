"""Private Supabase / PostgreSQL connection, kept in code (no environment variables).

KEEP THIS FILE PRIVATE: anyone who can read it can read and change your database.
Never upload this project to a public GitHub repository or share the ZIP publicly.
Leave DATABASE_URL empty ('') to go back to SQLite / the other config methods.
"""
DATABASE_URL = 'postgresql://postgres.eraqizraymcbjnxirwmr:Kvv7MSDkyqufhkgu@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres'
PG_SSLMODE = 'prefer'
