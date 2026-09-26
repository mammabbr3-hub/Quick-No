from aiogram import Router,Bot,F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.filters import Command
from decimal import Decimal
from sqlalchemy import select,func
from datetime import datetime,timezone
from app.db.session import SessionLocal
from app.db.models import User,Announcement,Broadcast,ScheduledMessage,Admin,MaintenanceState,PaymentMethod,Country,Deposit
from app.services.admin import is_admin,is_permanent,setting,set_setting,audit
from app.services.wallet import change_balance
from app.config import settings
router=Router()
async def copy_to_user(bot,user,src):
    try:
        if src.photo: await bot.send_photo(user.telegram_id,src.photo[-1].file_id,caption=src.caption or '')
        elif src.document: await bot.send_document(user.telegram_id,src.document.file_id,caption=src.caption or '')
        else: await bot.send_message(user.telegram_id,src.text or '')
        return True
    except Exception:return False
@router.message(Command('announce'))
async def announce(m:Message, bot:Bot):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        raw=(m.text or '').partition(' ')[2].strip()
        if raw.startswith('send '):
            parts=raw.split(maxsplit=2)
            if len(parts)<3 or not parts[1].isdigit(): return await m.answer('Usage: /announce send ID destination')
            aid=int(parts[1]); dest=parts[2].strip().lower(); aliases={'users':'bot_users','group':'official_group','channel':'official_channel'}; dest=aliases.get(dest,dest)
            a=await s.get(Announcement,aid)
            if not a:return await m.answer('Announcement not found.')
            if dest not in {'bot_users','official_group','official_channel'}:return await m.answer('Destination: bot_users | official_group | official_channel')
            if dest=='bot_users':
                targets=(await s.scalars(select(User.telegram_id).where(User.is_blocked.is_(False)))).all()
                await s.commit()
            else:
                key='official_group_id' if dest=='official_group' else 'official_channel_id'; cid=await setting(s,key,'')
                if not cid:return await m.answer('Infrastructure ID not configured.')
                targets=[int(cid)]; await s.commit()
        else:
            if '|' not in raw:return await m.answer('Usage: /announce Title | Body')
            title,body=[x.strip() for x in raw.split('|',1)]; a=Announcement(title=title,body=body,created_by=m.from_user.id); s.add(a); await s.commit(); return await m.answer(f'📢 Announcement saved as #{a.id}. Use /announce send {a.id} bot_users|official_group|official_channel')
    sent=failed=0
    for tg in targets:
        try: await bot.send_message(tg,f'📢 <b>{a.title}</b>\n\n{a.body}',parse_mode='HTML'); sent+=1
        except Exception: failed+=1
    await m.answer(f'📢 Announcement #{a.id} sent. Success: {sent}, Failed: {failed}')

@router.message(Command('broadcast'))
async def broadcast(m:Message, bot:Bot):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        raw=(m.text or '').partition(' ')[2].strip()
        src=m.reply_to_message or m
        if not (m.reply_to_message or raw):
            return await m.answer('Reply to text/photo/document with /broadcast DESTINATION, or /broadcast bot_users|text')
        if src.video or src.audio:
            return await m.answer('❌ Broadcasts support text, photo, or document only.')
        destination='bot_users'; content_override=None
        if raw and '|' in raw and not m.reply_to_message:
            destination,content_override=[x.strip() for x in raw.split('|',1)]
        elif raw and m.reply_to_message:
            destination=raw.split()[0].lower()
        aliases={'users':'bot_users','bot':'bot_users','bot_users':'bot_users','group':'official_group','channel':'official_channel'}
        destination=aliases.get(destination,destination)
        if destination not in {'bot_users','official_group','official_channel'}:
            return await m.answer('Destination: bot_users | official_group | official_channel')
        if destination!='bot_users':
            chat_key='official_group_id' if destination=='official_group' else 'official_channel_id'
            if not await setting(s,chat_key,''): return await m.answer('❌ Destination infrastructure ID is not configured.')
        if content_override is not None:
            ctype='text'; media_id=None; caption=None; content=content_override
        else:
            ctype='photo' if src.photo else 'document' if src.document else 'text'
            media_id=(src.photo[-1].file_id if src.photo else src.document.file_id if src.document else None)
            content=src.text or src.caption or ''; caption=src.caption
        total=int(await s.scalar(select(func.count(User.id)).where(User.is_blocked.is_(False))) or 0) if destination=='bot_users' else 1
        b=Broadcast(content_type=ctype,content=content,media_file_id=media_id,caption=caption,destination=destination,status='queued',total=total,created_by=m.from_user.id)
        s.add(b); await s.flush(); await audit(s,m.from_user.id,'broadcast_queued',str(b.id),new=destination); await s.commit()
    await m.answer(f'📣 Broadcast queued. ID: {b.id}\nDestination: {destination}\nRecipients: {total}')

@router.message(Command('schedule'))
async def schedule(m:Message):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id):return
        raw=(m.text or '').partition(' ')[2]
        parts=raw.split('|')
        if len(parts)<5:return await m.answer('Usage: /schedule Name | cron | timezone | destination | content')
        name,cron,tz,dest,content=[x.strip() for x in parts[:5]]
        s.add(ScheduledMessage(name=name,content_type='text',content=content,destination=dest,cron=cron,timezone=tz,created_by=m.from_user.id));await s.commit()
    await m.answer('⏰ Schedule created.')

@router.message(Command('block'))
async def block_user(m: Message):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        parts=(m.text or '').split(maxsplit=2)
        if len(parts)<3: return await m.answer('Usage: /block TELEGRAM_ID reason')
        try: tg=int(parts[1])
        except ValueError: return await m.answer('Invalid Telegram ID.')
        u=await s.scalar(select(User).where(User.telegram_id==tg))
        if not u:return await m.answer('User not found.')
        old='blocked' if u.is_blocked else 'active';u.is_blocked=True;u.block_reason=parts[2]
        await audit(s,m.from_user.id,'block_user',str(tg),old=old,new='blocked',reason=parts[2]);await s.commit()
    await m.answer(f'🚫 User {tg} blocked.')

@router.message(Command('unblock'))
async def unblock_user(m: Message):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        parts=(m.text or '').split(maxsplit=1)
        if len(parts)<2:return await m.answer('Usage: /unblock TELEGRAM_ID')
        try:tg=int(parts[1])
        except ValueError:return await m.answer('Invalid Telegram ID.')
        u=await s.scalar(select(User).where(User.telegram_id==tg))
        if not u:return await m.answer('User not found.')
        u.is_blocked=False;u.block_reason=None;await audit(s,m.from_user.id,'unblock_user',str(tg),old='blocked',new='active');await s.commit()
    await m.answer(f'✅ User {tg} unblocked.')

@router.message(Command('balance'))
async def balance_admin(m: Message):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        toks=(m.text or '').split(maxsplit=4)
        if len(toks)<5:return await m.answer('Usage: /balance TELEGRAM_ID add|deduct AMOUNT reason')
        try: tg=int(toks[1]); amount=Decimal(toks[3])
        except Exception:return await m.answer('Invalid Telegram ID or amount.')
        action=toks[2].lower(); reason=toks[4].strip()
        if action not in ('add','deduct') or amount<=0 or not reason:return await m.answer('Use add/deduct, a positive amount, and a reason.')
        u=await s.scalar(select(User).where(User.telegram_id==tg));
        if not u:return await m.answer('User not found.')
        w=await s.scalar(select(__import__('app.db.models',fromlist=['Wallet']).Wallet).where(__import__('app.db.models',fromlist=['Wallet']).Wallet.user_id==u.id))
        before=Decimal(w.balance or 0); after=before+(amount if action=='add' else -amount)
        if after<0:return await m.answer('Insufficient balance for deduction.')
        import base64,json
        payload=base64.urlsafe_b64encode(json.dumps({'tg':tg,'amount':str(amount),'action':action,'reason':reason}).encode()).decode().rstrip('=')
        await m.answer(f'⚠️ <b>Confirm balance adjustment</b>\n\nUser: <code>{tg}</code>\nAction: <b>{action}</b>\nAmount: <b>{amount:.2f} USDT</b>\nBefore: <b>{before:.2f}</b>\nAfter: <b>{after:.2f}</b>\nReason: {reason}',parse_mode='HTML',reply_markup=__import__('aiogram.types',fromlist=['InlineKeyboardMarkup','InlineKeyboardButton']).InlineKeyboardMarkup(inline_keyboard=[[__import__('aiogram.types',fromlist=['InlineKeyboardButton']).InlineKeyboardButton(text='✅ Confirm',callback_data='bconfirm:'+payload),__import__('aiogram.types',fromlist=['InlineKeyboardButton']).InlineKeyboardButton(text='❌ Cancel',callback_data='bcancel')]]))

@router.callback_query(F.data=='bcancel')
async def balance_cancel(c):
    async with SessionLocal() as s:
        if not await is_admin(s,c.from_user.id):return await c.answer('Unauthorized',show_alert=True)
    await c.message.edit_text('❌ Balance adjustment cancelled.'); await c.answer()

@router.callback_query(F.data.startswith('bconfirm:'))
async def balance_confirm(c):
    async with SessionLocal() as s:
        if not await is_admin(s,c.from_user.id):return await c.answer('Unauthorized',show_alert=True)
        import base64,json
        try:
            raw=c.data.split(':',1)[1]; raw += '='*(-len(raw)%4); data=json.loads(base64.urlsafe_b64decode(raw).decode()); tg=int(data['tg']); amount=Decimal(data['amount']); action=data['action']; reason=data['reason']
        except Exception:return await c.answer('Invalid confirmation.',show_alert=True)
        u=await s.scalar(select(User).where(User.telegram_id==tg))
        if not u:return await c.answer('User not found.',show_alert=True)
        delta=amount if action=='add' else -amount
        try: _,before,after=await change_balance(s,u.id,delta,'manual_add' if action=='add' else 'manual_deduct',None,c.from_user.id)
        except ValueError as e:return await c.answer(str(e),show_alert=True)
        await audit(s,c.from_user.id,'manual_balance',str(tg),old=str(before),new=str(after),reason=reason); await s.commit()
    await c.message.edit_text(f'✅ Balance updated. User <code>{tg}</code>: <b>{after:.2f} USDT</b>',parse_mode='HTML'); await c.answer('Confirmed')

@router.message(Command('admin_add'))
async def admin_add(m: Message):
    async with SessionLocal() as s:
        if not await is_permanent(m.from_user.id): return
        toks=(m.text or '').split(maxsplit=2)
        if len(toks)<2:return await m.answer('Usage: /admin_add TELEGRAM_ID [username]')
        try:tg=int(toks[1])
        except ValueError:return await m.answer('Invalid Telegram ID.')
        username=toks[2].lstrip('@') if len(toks)>2 else None
        existing=await s.scalar(select(Admin).where(Admin.telegram_id==tg))
        if existing:
            existing.is_active=True; existing.username=username or existing.username
        else:s.add(Admin(telegram_id=tg,username=username,is_active=True))
        await audit(s,m.from_user.id,'add_admin',str(tg),new='active');await s.commit()
    await m.answer(f'👑 Admin {tg} is active.')

@router.message(Command('admin_remove'))
async def admin_remove(m: Message):
    async with SessionLocal() as s:
        if not await is_permanent(m.from_user.id): return
        toks=(m.text or '').split(maxsplit=1)
        if len(toks)<2:return await m.answer('Usage: /admin_remove TELEGRAM_ID')
        try:tg=int(toks[1])
        except ValueError:return await m.answer('Invalid Telegram ID.')
        if tg==m.from_user.id or tg==settings.permanent_admin_id:return await m.answer('The permanent admin cannot be removed.')
        a=await s.scalar(select(Admin).where(Admin.telegram_id==tg))
        if not a:return await m.answer('Admin not found.')
        a.is_active=False;await audit(s,m.from_user.id,'remove_admin',str(tg),old='active',new='inactive');await s.commit()
    await m.answer(f'🚫 Admin {tg} removed.')

@router.message(Command('setinfra'))
async def set_infra(m: Message):
    async with SessionLocal() as s:
        if not await is_permanent(m.from_user.id): return
        toks=(m.text or '').split(maxsplit=2)
        allowed={'group_id':'official_group_id','channel_id':'official_channel_id','log_group_id':'admin_log_group_id'}
        if len(toks)<3 or toks[1] not in allowed:
            return await m.answer('Usage: /setinfra group_id|channel_id|log_group_id VALUE')
        key=allowed[toks[1]]; value=toks[2].strip()
        old=await setting(s,key,''); await set_setting(s,key,value,m.from_user.id)
        await audit(s,m.from_user.id,'set_infrastructure_id',key,old=old,new=value);await s.commit()
    await m.answer(f'⚙️ {key} updated.')

@router.message(Command('setsetting'))
async def set_setting_cmd(m: Message):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        toks=(m.text or '').split(maxsplit=2)
        allowed={'support_username','daily_report_time','daily_report_timezone','maintenance_message','official_group_url','official_channel_url'}
        if len(toks)<3 or toks[1] not in allowed:
            return await m.answer('Allowed: support_username, daily_report_time, daily_report_timezone, maintenance_message, official_group_url, official_channel_url')
        key,value=toks[1],toks[2].strip(); old=await setting(s,key,'');await set_setting(s,key,value,m.from_user.id);await s.commit()
    await m.answer(f'⚙️ {key} updated.')

@router.message(Command('maintenance'))
async def maintenance_cmd(m: Message):
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        toks=(m.text or '').split(maxsplit=2)
        if len(toks)<2 or toks[1].lower() not in ('on','off'):
            return await m.answer('Usage: /maintenance on|off [message]')
        state=await s.scalar(select(MaintenanceState).where(MaintenanceState.id==1))
        state.enabled=toks[1].lower()=='on'
        if len(toks)>2:state.message=toks[2]
        state.updated_by=m.from_user.id
        await audit(s,m.from_user.id,'maintenance',str(state.enabled),new=state.message);await s.commit()
    await m.answer('🔧 Maintenance '+('enabled.' if state.enabled else 'disabled.'))


@router.message(Command('payment_method'))
async def payment_method_cmd(m: Message):
    """Admin payment method CRUD. /payment_method add|disable|enable|delete ..."""
    async with SessionLocal() as s:
        if not await is_admin(s, m.from_user.id): return
        parts=(m.text or '').split(maxsplit=3)
        if len(parts)<2:
            return await m.answer('Usage: /payment_method add NAME | CURRENCY | RATE | MIN | DETAILS | INSTRUCTIONS\n/payment_method enable ID\n/payment_method disable ID\n/payment_method delete ID')
        action=parts[1].lower()
        if action in ('enable','disable','delete'):
            if len(parts)<3 or not parts[2].isdigit(): return await m.answer('Invalid payment method ID.')
            obj=await s.get(PaymentMethod,int(parts[2]))
            if not obj:return await m.answer('Payment method not found.')
            if action=='enable':obj.enabled=True
            elif action=='disable':obj.enabled=False
            else:
                linked = await s.scalar(select(Deposit.id).where(Deposit.payment_method_id == obj.id).limit(1))
                if linked:
                    return await m.answer('❌ This payment method has historical deposits. Disable it instead of deleting it so old payment/account details remain auditable.')
                await s.delete(obj)
            await audit(s,m.from_user.id,f'payment_method_{action}',str(obj.id)); await s.commit(); return await m.answer(f'💳 Payment method {action}d.')
        if action!='add' or len(parts)<3:return await m.answer('Invalid action.')
        fields=[x.strip() for x in parts[2].split('|')]
        if len(fields)<6:return await m.answer('Need NAME | CURRENCY | RATE | MIN | DETAILS | INSTRUCTIONS')
        try: rate=Decimal(fields[2]); minimum=Decimal(fields[3])
        except Exception:return await m.answer('Invalid rate/minimum.')
        obj=PaymentMethod(name=fields[0],currency=fields[1],exchange_rate=rate,min_deposit=minimum,details=fields[4],instructions=fields[5],enabled=True)
        s.add(obj); await s.flush(); await audit(s,m.from_user.id,'payment_method_add',str(obj.id),new=obj.name); await s.commit()
    await m.answer(f'✅ Payment method created: #{obj.id} {obj.name}')

@router.message(Command('country'))
async def country_cmd(m: Message):
    """Country/pricing CRUD. /country add CODE | NAME | FLAG | SERVICE | COST | PERCENT | FIXED | EXPLICIT"""
    async with SessionLocal() as s:
        if not await is_admin(s,m.from_user.id): return
        parts=(m.text or '').split(maxsplit=2)
        if len(parts)<2:return await m.answer('Usage: /country add CODE | NAME | FLAG | SERVICE | COST | PERCENT | FIXED | EXPLICIT\n/country enable CODE\n/country disable CODE')
        action=parts[1].lower()
        if action in ('enable','disable'):
            if len(parts)<3:return await m.answer('Country code required.')
            obj=await s.scalar(select(Country).where(Country.code==parts[2].strip()))
            if not obj:return await m.answer('Country not found.')
            obj.enabled=action=='enable'; await audit(s,m.from_user.id,f'country_{action}',obj.code,new=str(obj.enabled)); await s.commit(); return await m.answer(f'🌍 {obj.code}: {action}d.')
        if action!='add' or len(parts)<3:return await m.answer('Invalid action.')
        f=[x.strip() for x in parts[2].split('|')]
        if len(f)<8:return await m.answer('Need CODE | NAME | FLAG | SERVICE | COST | PERCENT | FIXED | EXPLICIT (use - for none)')
        try:
            cost=None if f[4]=='-' else Decimal(f[4]); percent=Decimal(f[5]); fixed=Decimal(f[6]); explicit=None if f[7]=='-' else Decimal(f[7])
        except Exception:return await m.answer('Invalid pricing values.')
        obj=await s.scalar(select(Country).where(Country.code==f[0]))
        if obj:
            obj.name=f[1];obj.flag=f[2];obj.service_code=f[3];obj.grizzly_cost=cost;obj.markup_percent=percent;obj.markup_fixed=fixed;obj.explicit_price=explicit;obj.enabled=True
            action_name='country_update'
        else:
            obj=Country(code=f[0],name=f[1],flag=f[2],service_code=f[3],grizzly_cost=cost,markup_percent=percent,markup_fixed=fixed,explicit_price=explicit,enabled=True);s.add(obj);action_name='country_add'
        await s.flush(); await audit(s,m.from_user.id,action_name,obj.code,new=f[1]); await s.commit()
    await m.answer(f'✅ Country {obj.code} configured. Price: {obj.grizzly_cost}')

@router.message(Command('deposit'))
async def deposit_admin(m: Message, bot: Bot):
    async with SessionLocal() as s:
        if not await is_admin(s, m.from_user.id): return
        toks=(m.text or '').split(maxsplit=3)
        if len(toks) < 3:
            return await m.answer('Usage: /deposit list | /deposit approve ID | /deposit reject ID reason')
        action=toks[1].lower()
        if action == 'list':
            rows=(await s.scalars(select(Deposit).where(Deposit.status.in_(['pending','expired'])).order_by(Deposit.created_at.asc()).limit(50))).all()
            if not rows: return await m.answer('💳 No pending deposits.')
            return await m.answer('💳 <b>Pending deposits</b>\n\n'+'\n'.join(f'#{d.id} <code>{d.reference}</code> — {d.usdt_amount:.2f} USDT — {d.status}' for d in rows),parse_mode='HTML')
        if not toks[2].isdigit(): return await m.answer('Invalid deposit ID.')
        did=int(toks[2])
        from app.services.deposits import approve_deposit, reject_deposit
        try:
            if action=='approve':
                d=await approve_deposit(s,did,m.from_user.id); await audit(s,m.from_user.id,'deposit_approve',d.reference,new='approved'); await s.commit()
                u=await s.scalar(select(User).where(User.id==d.user_id))
                if u:
                    try: await bot.send_message(u.telegram_id,f'✅ <b>Deposit approved</b>\n\nReference: <code>{d.reference}</code>\nAmount: <b>{d.usdt_amount:.2f} USDT</b>',parse_mode='HTML')
                    except Exception: pass
                return await m.answer(f'✅ Deposit {d.reference} approved.')
            if action=='reject':
                if len(toks)<4:return await m.answer('Usage: /deposit reject ID reason')
                d=await reject_deposit(s,did,m.from_user.id,toks[3]); await audit(s,m.from_user.id,'deposit_reject',d.reference,new='rejected',reason=d.rejection_reason); await s.commit()
                u=await s.scalar(select(User).where(User.id==d.user_id))
                if u:
                    try: await bot.send_message(u.telegram_id,f'❌ <b>Deposit rejected</b>\n\nReference: <code>{d.reference}</code>\nReason: {d.rejection_reason}',parse_mode='HTML')
                    except Exception: pass
                return await m.answer(f'❌ Deposit {d.reference} rejected.')
        except ValueError as e:
            await s.rollback(); return await m.answer(f'⚠️ {e.args[0]}')
        return await m.answer('Invalid action. Use list, approve or reject.')
