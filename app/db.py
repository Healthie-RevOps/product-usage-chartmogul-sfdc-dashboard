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

    # Enterprise attributes (sync/sfdc.py ACCOUNT_ENTERPRISE_FIELDS). SFDC's own values,
    # matching what the SFDC "Enterprise Customer Attribute Reporting" report shows.
    current_plan = Column(String, index=True)            # Current_Plan__c (e.g. Enterprise)
    customer_status = Column(String)                     # Customer_Status__c
    sfdc_mrr = Column(Float)                             # MRR__c (dollars)
    implementation_status = Column(String)
    csm_name = Column(String)
    website = Column(String)
    renewal_date = Column(Date)
    healthie_org_count = Column(Float)
    account_segment = Column(String)                     # Strategic / Growth / Foundational
    enterprise_category = Column(String)                 # multi-select, ";"-separated
    current_ehr = Column(String)
    kickoff_date = Column(Date)

    # Add-on flags (Stripe_Has_* checkboxes, synced into SFDC from Stripe)
    has_active_client = Column(Boolean, default=False)
    has_sub_org = Column(Boolean, default=False)
    has_ai_scribe = Column(Boolean, default=False)
    has_api_access = Column(Boolean, default=False)
    has_erx = Column(Boolean, default=False)
    has_full_white_label = Column(Boolean, default=False)
    has_semi_white_label = Column(Boolean, default=False)
    has_hubspot = Column(Boolean, default=False)
    has_shopify = Column(Boolean, default=False)
    has_standard_seats = Column(Boolean, default=False)
    has_support_seats = Column(Boolean, default=False)
    has_zoom = Column(Boolean, default=False)

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
    email_opt_out = Column(Boolean, default=False)            # HasOptedOutOfEmail

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


class SfdcOpportunity(Base):
    """Every open Opportunity plus every closed one since SFDC_OPPS_CLOSED_SINCE, any stage
    or record type, whether or not the account matches a ChartMogul customer (lost
    prospects never do). No FK to sfdc_accounts: most of these accounts aren't synced,
    so the account name is stored here. Raw SFDC values; product detection and
    competitor normalization happen in the app so they can change without a resync."""
    __tablename__ = "sfdc_opportunities"

    id = Column(String, primary_key=True)                # Opportunity Id
    account_id = Column(String, index=True)
    account_name = Column(String)
    name = Column(String)
    record_type = Column(String, index=True)             # Initial Sales / Expansion Deal / ...
    opp_type = Column(String, index=True)                # New Deal / Add-on / Expansion / ...
    stage = Column(String, index=True)
    is_closed = Column(Boolean, default=False)
    is_won = Column(Boolean, default=False)
    close_date = Column(Date, index=True)
    created_date = Column(DateTime)
    amount = Column(Float)
    probability = Column(Float)                          # percent, 0-100
    next_step = Column(Text)
    owner_name = Column(String)

    year1_acv = Column(Float)                            # Year_1_ACV__c
    arr_delta = Column(Float)                            # ARR_Delta__c
    loss_reason = Column(String)                         # Loss_Reason__c
    loss_reason_detail = Column(Text)
    addon_loss_reason = Column(String)                   # Add_On_Close_Lost_Reason__c
    why_won = Column(Text)
    competitor = Column(String)                          # Competitor__c
    secondary_competitor = Column(String)
    competitor_text = Column(String)                     # Competitor_Free_Text__c
    incumbent = Column(String)
    competition = Column(String)
    current_situation = Column(String)
    expansion_product = Column(String)                   # Expansion_Product__c (mostly blank)
    expansion_product_text = Column(String)
    additional_products = Column(String)
    target_price = Column(Float)                         # Customer_Target_Price__c
    value_gap = Column(Text)                             # Perceived_Value_Gap__c
    revisit_date = Column(Date)                          # Revisit_Later_Date__c


class HealthieOrgUsage(Base):
    """Org-level product usage from Healthie's warehouse via a saved Metabase question
    (sync/metabase.py). Aggregates only, never person-level rows. Joined to SFDC
    accounts on Healthie_Org_ID__c. Refreshed separately from the nightly snapshot, and
    kept as-is when Metabase isn't configured."""
    __tablename__ = "healthie_org_usage"

    org_id = Column(String, primary_key=True)            # Healthie organization ID
    providers = Column(Integer)                          # excl. test + Healthie-employee users
    active_providers = Column(Integer)                   # PROVIDER_IS_ACTIVE (not deactivated)
    admins = Column(Integer)
    last_provider_activity = Column(Date)                # most recent provider login, any
    clients = Column(Integer)                            # patients, excl. test users
    active_clients = Column(Integer)                     # CLIENT_IS_ACTIVE
    synced_at = Column(DateTime, server_default=func.now())


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
    opportunities_synced = Column(Integer, default=0)
    # ChartMogul stage: "full" or "incremental"; completed_at = its transaction committed.
    # The next incremental refresh reads ChartMogul activity since this run started.
    cm_mode = Column(String)
    cm_completed_at = Column(DateTime)
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
