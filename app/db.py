"""Database layer. Railway provides DATABASE_URL automatically when you attach Postgres."""
import os
from contextlib import contextmanager

from sqlalchemy import (
    Boolean, Column, Date, Float, String, Integer, BigInteger, DateTime, Text, ForeignKey,
    create_engine, func, inspect, text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///local.db")
# Railway's Postgres URL sometimes comes as postgres:// which SQLAlchemy 2.x rejects
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


class Customer(Base):
    __tablename__ = "customers"

    cm_uuid = Column(String, primary_key=True)          # ChartMogul customer UUID
    name = Column(String)
    email = Column(String)
    status = Column(String)                              # Lead / Active / Past Due / Cancelled
    customer_since = Column(DateTime)
    mrr_cents = Column(BigInteger, default=0)            # total current MRR in cents
    currency = Column(String, default="USD")
    stripe_customer_id = Column(String, index=True)      # from ChartMogul external_ids
    healthie_org_id = Column(String, index=True)         # from CM custom attributes, if set
    cm_url = Column(String)

    # SFDC join results
    sfdc_account_id = Column(String, ForeignKey("sfdc_accounts.id"), nullable=True, index=True)
    match_method = Column(String)                        # stripe_id | org_id | unmatched

    synced_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    subscriptions = relationship("Subscription", back_populates="customer",
                                 cascade="all, delete-orphan")
    activities = relationship("Activity", back_populates="customer",
                              cascade="all, delete-orphan", order_by="Activity.date")
    sfdc_account = relationship("SfdcAccount", back_populates="customers")


class Subscription(Base):
    __tablename__ = "subscriptions"

    cm_uuid = Column(String, primary_key=True)           # ChartMogul subscription UUID
    customer_uuid = Column(String, ForeignKey("customers.cm_uuid"), index=True)
    plan_name = Column(String, index=True)
    product = Column(String, index=True)                 # family: plan name before " - "
    external_id = Column(String)                         # Stripe subscription id
    status = Column(String)                              # active / cancelled / etc.
    is_current = Column(Boolean, default=False, index=True)   # live today (see run_sync)
    quantity = Column(Integer)
    mrr_cents = Column(BigInteger, default=0)
    currency = Column(String, default="USD")
    billing_cycle = Column(String)                       # month / year
    start_date = Column(DateTime)
    end_date = Column(DateTime)

    customer = relationship("Customer", back_populates="subscriptions")


class Activity(Base):
    """ChartMogul MRR movement for one customer (new_biz / expansion / contraction /
    churn / reactivation). `mrr_cents` is the customer's total MRR *after* the event,
    so the series is the customer's revenue history. A plan switch is one net movement
    on the new plan, so don't sum movements by product; use ProductMrr for that."""
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_uuid = Column(String, ForeignKey("customers.cm_uuid"), index=True)
    date = Column(DateTime, index=True)
    type = Column(String)
    mrr_cents = Column(BigInteger)
    movement_cents = Column(BigInteger)
    plan_external_id = Column(String)
    plan_name = Column(String)
    product = Column(String, index=True)
    subscription_external_id = Column(String)
    description = Column(Text)

    customer = relationship("Customer", back_populates="activities")


class ProductMrr(Base):
    """Monthly MRR per product, from ChartMogul's metrics API filtered to the product's
    plans (ChartMogul-computed, so plan switches are attributed correctly)."""
    __tablename__ = "product_mrr"

    product = Column(String, primary_key=True)
    month = Column(Date, primary_key=True)
    mrr_cents = Column(BigInteger, default=0)
    new_biz_cents = Column(BigInteger, default=0)
    expansion_cents = Column(BigInteger, default=0)
    contraction_cents = Column(BigInteger, default=0)
    churn_cents = Column(BigInteger, default=0)
    reactivation_cents = Column(BigInteger, default=0)


class SfdcAccount(Base):
    __tablename__ = "sfdc_accounts"

    id = Column(String, primary_key=True)                # 18-char SFDC Id
    name = Column(String)
    healthie_org_id = Column(String, index=True)
    stripe_customer_id = Column(String, index=True)
    owner_name = Column(String)
    account_type = Column(String)

    # Usage signals synced into SFDC (largely from Vitally). Fill rates vary a lot —
    # see README "Data quality" before trusting any single one.
    active_standard_users = Column(Float)                # Active_Standard_Users_Account__c
    active_clients = Column(Float)                       # Healthie_Active_Clients_Account__c
    num_providers = Column(Float)                        # Number_Of_Providers__c
    adoption_score = Column(Float)                       # Product_Adoption_Score__c
    health_score = Column(String)                        # Customer_Health_Score__c
    sfdc_arr = Column(Float)                             # ARR__c (dollars)

    customers = relationship("Customer", back_populates="sfdc_account")
    contacts = relationship("SfdcContact", back_populates="account",
                            cascade="all, delete-orphan")
    contracts = relationship("SfdcContract", back_populates="account",
                             cascade="all, delete-orphan",
                             order_by="SfdcContract.close_date.desc()")


class SfdcContact(Base):
    __tablename__ = "sfdc_contacts"

    id = Column(String, primary_key=True)
    account_id = Column(String, ForeignKey("sfdc_accounts.id"), index=True)
    name = Column(String)
    email = Column(String)
    title = Column(String)
    phone = Column(String)
    mobile_phone = Column(String)
    healthie_user_id = Column(String)
    user_active = Column(Boolean, default=False, index=True)   # Healthie_User_Active__c
    user_type = Column(String)                                 # Standard / Support
    last_active = Column(DateTime)                             # stale since mid-2024
    is_org_admin = Column(Boolean, default=False)
    is_org_owner = Column(Boolean, default=False)

    account = relationship("SfdcAccount", back_populates="contacts")


class SfdcContract(Base):
    """Closed-won Opportunities carrying contract terms. SFDC is the system of record
    for contract value; ChartMogul MRR is what is actually being billed today."""
    __tablename__ = "sfdc_contracts"

    id = Column(String, primary_key=True)                # Opportunity Id
    account_id = Column(String, ForeignKey("sfdc_accounts.id"), index=True)
    name = Column(String)
    opp_type = Column(String)
    close_date = Column(Date)
    amount = Column(Float)
    total_contract_value = Column(Float)                 # Total_Contract_Value__c
    year1_acv = Column(Float)                            # Year_1_ACV__c
    contract_start = Column(Date)
    contract_end = Column(Date)
    term_months = Column(Float)

    account = relationship("SfdcAccount", back_populates="contracts")

    @property
    def value(self):
        """Best available contract value: TCV, else Year-1 ACV, else Amount."""
        return self.total_contract_value or self.year1_acv or self.amount


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime)
    status = Column(String, default="running")           # running / success / failed
    customers_synced = Column(Integer, default=0)
    subscriptions_synced = Column(Integer, default=0)
    accounts_synced = Column(Integer, default=0)
    contacts_synced = Column(Integer, default=0)
    contracts_synced = Column(Integer, default=0)
    matched = Column(Integer, default=0)
    unmatched = Column(Integer, default=0)
    error = Column(Text)


def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns():
    """create_all() never alters existing tables. Every table here is truncated and
    reloaded by the sync, so adding new nullable columns in place is a safe migration."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    coltype = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(
                        f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype}'))


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
