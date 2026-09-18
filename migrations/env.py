"""Alembic 환경 설정.

DB URL은 DATABASE_URL 환경변수에서 읽는다(app.db 와 동일 기본값).
현재는 ORM 모델 autogenerate 없이 손으로 작성한 마이그레이션만 사용하므로
target_metadata 는 None 이다(추후 모델 도입 시 연결).
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://pixlate:pixlate_dev_pw@pixlate-db:5432/pixlate",
)
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
