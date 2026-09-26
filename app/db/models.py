from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Index
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base

def now(): return datetime.now(timezone.utc)

class UTCDateTime(TypeDecorator):
    """SQLite-safe timezone-aware UTC datetime type."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(Integer,primary_key=True)
    telegram_id: Mapped[int]=mapped_column(BigInteger,unique=True,index=True)
    username: Mapped[str|None]=mapped_column(String(255),index=True)
    first_name: Mapped[str|None]=mapped_column(String(255)); last_name: Mapped[str|None]=mapped_column(String(255))
    display_name: Mapped[str]=mapped_column(String(255)); is_blocked: Mapped[bool]=mapped_column(Boolean,default=False,index=True)
    block_reason: Mapped[str|None]=mapped_column(Text); joined_gate: Mapped[bool]=mapped_column(Boolean,default=False)
    total_deposits: Mapped[Decimal]=mapped_column(Numeric(20,8),default=0); total_spending: Mapped[Decimal]=mapped_column(Numeric(20,8),default=0)
    total_orders: Mapped[int]=mapped_column(Integer,default=0); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,index=True)
    last_activity_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,index=True)

class Wallet(Base):
    __tablename__='wallets'; id: Mapped[int]=mapped_column(Integer,primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE'),unique=True,index=True)
    balance: Mapped[Decimal]=mapped_column(Numeric(20,8),default=0); updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,onupdate=now)

class Admin(Base):
    __tablename__='admins'; id: Mapped[int]=mapped_column(Integer,primary_key=True); telegram_id: Mapped[int]=mapped_column(BigInteger,unique=True,index=True)
    username: Mapped[str|None]=mapped_column(String(255)); is_active: Mapped[bool]=mapped_column(Boolean,default=True); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now)

class SystemSetting(Base):
    __tablename__='system_settings'; key: Mapped[str]=mapped_column(String(120),primary_key=True); value: Mapped[str]=mapped_column(Text)
    updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,onupdate=now); updated_by: Mapped[int|None]=mapped_column(BigInteger)

class Country(Base):
    __tablename__='countries'; id: Mapped[int]=mapped_column(Integer,primary_key=True); code: Mapped[str]=mapped_column(String(32),unique=True,index=True)
    name: Mapped[str]=mapped_column(String(120)); flag: Mapped[str]=mapped_column(String(8),default='🌍'); enabled: Mapped[bool]=mapped_column(Boolean,default=False,index=True)
    service_code: Mapped[str]=mapped_column(String(32),default=''); grizzly_cost: Mapped[Decimal|None]=mapped_column(Numeric(20,8)); markup_percent: Mapped[Decimal]=mapped_column(Numeric(12,4),default=0)
    markup_fixed: Mapped[Decimal]=mapped_column(Numeric(20,8),default=0); explicit_price: Mapped[Decimal|None]=mapped_column(Numeric(20,8)); updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,onupdate=now)

class PaymentMethod(Base):
    __tablename__='payment_methods'; id: Mapped[int]=mapped_column(Integer,primary_key=True); name: Mapped[str]=mapped_column(String(120)); country: Mapped[str]=mapped_column(String(120),default='')
    currency: Mapped[str]=mapped_column(String(16),default='NGN'); exchange_rate: Mapped[Decimal]=mapped_column(Numeric(20,8),default=1); min_deposit: Mapped[Decimal]=mapped_column(Numeric(20,8),default=3)
    details: Mapped[str]=mapped_column(Text,default=''); instructions: Mapped[str]=mapped_column(Text,default=''); enabled: Mapped[bool]=mapped_column(Boolean,default=True,index=True); display_order: Mapped[int]=mapped_column(Integer,default=0)

class Deposit(Base):
    __tablename__='deposits'; id: Mapped[int]=mapped_column(Integer,primary_key=True); reference: Mapped[str]=mapped_column(String(40),unique=True,index=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True); payment_method_id: Mapped[int]=mapped_column(ForeignKey('payment_methods.id')); usdt_amount: Mapped[Decimal]=mapped_column(Numeric(20,8))
    local_amount: Mapped[Decimal]=mapped_column(Numeric(20,8)); frozen_rate: Mapped[Decimal]=mapped_column(Numeric(20,8)); status: Mapped[str]=mapped_column(String(24),default='pending',index=True)
    payment_method_name_snapshot: Mapped[str]=mapped_column(String(120),default='')
    payment_country_snapshot: Mapped[str]=mapped_column(String(120),default='')
    payment_currency_snapshot: Mapped[str]=mapped_column(String(16),default='')
    payment_details_snapshot: Mapped[str]=mapped_column(Text,default='')
    payment_instructions_snapshot: Mapped[str]=mapped_column(Text,default='')
    rejection_reason: Mapped[str|None]=mapped_column(Text); expires_at: Mapped[datetime]=mapped_column(UTCDateTime(),index=True); approved_by: Mapped[int|None]=mapped_column(BigInteger)
    created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,index=True); updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,onupdate=now)

class PaymentProof(Base):
    __tablename__='payment_proofs'; id: Mapped[int]=mapped_column(Integer,primary_key=True); deposit_id: Mapped[int]=mapped_column(ForeignKey('deposits.id'),index=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True); telegram_file_id: Mapped[str]=mapped_column(Text); file_unique_id: Mapped[str|None]=mapped_column(String(255),index=True)
    sha256: Mapped[str|None]=mapped_column(String(64),index=True); possible_duplicate: Mapped[bool]=mapped_column(Boolean,default=False,index=True); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now)

class LedgerTransaction(Base):
    __tablename__='ledger_transactions'; id: Mapped[int]=mapped_column(Integer,primary_key=True); transaction_id: Mapped[str]=mapped_column(String(40),unique=True,index=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True); type: Mapped[str]=mapped_column(String(32),index=True); amount: Mapped[Decimal]=mapped_column(Numeric(20,8))
    balance_before: Mapped[Decimal]=mapped_column(Numeric(20,8)); balance_after: Mapped[Decimal]=mapped_column(Numeric(20,8)); reference: Mapped[str|None]=mapped_column(String(120),index=True)
    status: Mapped[str]=mapped_column(String(24),default='completed'); actor_id: Mapped[int|None]=mapped_column(BigInteger); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,index=True)

class Order(Base):
    __tablename__='orders'; id: Mapped[int]=mapped_column(Integer,primary_key=True); order_id: Mapped[str]=mapped_column(String(40),unique=True,index=True); user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    country_code: Mapped[str]=mapped_column(String(32),index=True); country_name: Mapped[str]=mapped_column(String(120)); service_code: Mapped[str]=mapped_column(String(32)); status: Mapped[str]=mapped_column(String(32),default='pending',index=True)
    phone_number: Mapped[str|None]=mapped_column(String(64)); activation_id: Mapped[str|None]=mapped_column(String(128),unique=True,index=True); raw_cost: Mapped[Decimal|None]=mapped_column(Numeric(20,8)); selling_price: Mapped[Decimal]=mapped_column(Numeric(20,8)); profit: Mapped[Decimal|None]=mapped_column(Numeric(20,8))
    otp_code: Mapped[str|None]=mapped_column(String(64)); refunded: Mapped[bool]=mapped_column(Boolean,default=False,index=True); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,index=True); updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,onupdate=now)

class OrderEvent(Base):
    __tablename__='order_events'; id: Mapped[int]=mapped_column(Integer,primary_key=True); order_id: Mapped[int]=mapped_column(ForeignKey('orders.id',ondelete='CASCADE'),index=True); event_type: Mapped[str]=mapped_column(String(64),index=True); data: Mapped[str]=mapped_column(Text,default='{}'); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now)

class AdminAuditLog(Base):
    __tablename__='admin_audit_logs'; id: Mapped[int]=mapped_column(Integer,primary_key=True); admin_id: Mapped[int]=mapped_column(BigInteger,index=True); action: Mapped[str]=mapped_column(String(120),index=True); target: Mapped[str|None]=mapped_column(String(255)); old_value: Mapped[str|None]=mapped_column(Text); new_value: Mapped[str|None]=mapped_column(Text); reason: Mapped[str|None]=mapped_column(Text); result: Mapped[str]=mapped_column(String(32),default='success'); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,index=True)

class Announcement(Base):
    __tablename__='announcements'; id: Mapped[int]=mapped_column(Integer,primary_key=True); title: Mapped[str]=mapped_column(String(255)); body: Mapped[str]=mapped_column(Text); enabled: Mapped[bool]=mapped_column(Boolean,default=True); created_by: Mapped[int]=mapped_column(BigInteger); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,index=True)

class Broadcast(Base):
    __tablename__='broadcasts'; id: Mapped[int]=mapped_column(Integer,primary_key=True); content_type: Mapped[str]=mapped_column(String(16)); content: Mapped[str]=mapped_column(Text); media_file_id: Mapped[str|None]=mapped_column(Text); caption: Mapped[str|None]=mapped_column(Text); destination: Mapped[str]=mapped_column(String(32)); status: Mapped[str]=mapped_column(String(24),default='queued'); total: Mapped[int]=mapped_column(Integer,default=0); sent: Mapped[int]=mapped_column(Integer,default=0); failed: Mapped[int]=mapped_column(Integer,default=0); created_by: Mapped[int]=mapped_column(BigInteger); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now); finished_at: Mapped[datetime|None]=mapped_column(UTCDateTime())

class ScheduledMessage(Base):
    __tablename__='scheduled_messages'; id: Mapped[int]=mapped_column(Integer,primary_key=True); name: Mapped[str]=mapped_column(String(160)); content_type: Mapped[str]=mapped_column(String(16)); content: Mapped[str]=mapped_column(Text); media_file_id: Mapped[str|None]=mapped_column(Text); caption: Mapped[str|None]=mapped_column(Text); destination: Mapped[str]=mapped_column(String(32)); cron: Mapped[str]=mapped_column(String(120)); timezone: Mapped[str]=mapped_column(String(64),default='UTC'); enabled: Mapped[bool]=mapped_column(Boolean,default=True,index=True); last_run_at: Mapped[datetime|None]=mapped_column(UTCDateTime()); created_by: Mapped[int]=mapped_column(BigInteger); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now)

class ScheduledMessageRun(Base):
    __tablename__='scheduled_message_runs'
    id: Mapped[int]=mapped_column(Integer,primary_key=True)
    schedule_id: Mapped[int]=mapped_column(ForeignKey('scheduled_messages.id',ondelete='CASCADE'),index=True)
    scheduled_for: Mapped[datetime]=mapped_column(UTCDateTime(),index=True)
    status: Mapped[str]=mapped_column(String(24),default='running',index=True)
    sent: Mapped[int]=mapped_column(Integer,default=0)
    failed: Mapped[int]=mapped_column(Integer,default=0)
    error: Mapped[str|None]=mapped_column(Text)
    started_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now)
    finished_at: Mapped[datetime|None]=mapped_column(UTCDateTime())

class DailyReport(Base):
    __tablename__='daily_reports'; id: Mapped[int]=mapped_column(Integer,primary_key=True); report_date: Mapped[str]=mapped_column(String(10),unique=True,index=True); payload: Mapped[str]=mapped_column(Text); created_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now)

class MaintenanceState(Base):
    __tablename__='maintenance_state'; id: Mapped[int]=mapped_column(Integer,primary_key=True,default=1); enabled: Mapped[bool]=mapped_column(Boolean,default=False); message: Mapped[str]=mapped_column(Text,default='Maintenance in progress. Please try again later.'); updated_by: Mapped[int|None]=mapped_column(BigInteger); updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,onupdate=now)

class GrizzlyActivation(Base):
    __tablename__='grizzly_activations'; id: Mapped[int]=mapped_column(Integer,primary_key=True); activation_id: Mapped[str]=mapped_column(String(128),unique=True,index=True); order_id: Mapped[int]=mapped_column(ForeignKey('orders.id'),unique=True,index=True); last_raw: Mapped[str|None]=mapped_column(Text); last_status: Mapped[str|None]=mapped_column(String(64)); updated_at: Mapped[datetime]=mapped_column(UTCDateTime(),default=now,onupdate=now)

Index('ix_deposit_user_status',Deposit.user_id,Deposit.status); Index('ix_order_user_status',Order.user_id,Order.status)
