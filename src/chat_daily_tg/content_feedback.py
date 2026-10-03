"""Owner reply intake for the existing consumer; durable retries and exact mapping."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

from chat_daily_tg.call_receipts import digest
from chat_daily_tg.intent_feedback import FeedbackStore

COMMANDS={'展开':'expand','少推这类':'filter'}


def read_rows(path):
    path=Path(path)
    if not path.exists():
        return []
    rows=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row=json.loads(line)
            if isinstance(row,dict): rows.append(row)
        except ValueError:
            continue
    return rows


def public_url(value):
    p=urlparse(str(value))
    return p.scheme=='https' and p.hostname=='t.me' and not p.path.startswith(('/c/','/+','/joinchat/')) and len(p.path.strip('/').split('/'))>=2


class ReplyIntake:
    def __init__(self, root, *, bot_id, owner_id, targets, text_ledger, media_ledger=None, originals=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.bot_id=str(bot_id);self.owner_id=str(owner_id)
        if not self.bot_id.isdigit() or not self.owner_id.isdigit():
            raise ValueError('numeric bot and owner identities required')
        self.targets={(str(chat),str(topic) if topic is not None else None) for chat,topic in targets}
        if not self.targets: raise ValueError('reply target whitelist required')
        self.text_ledger=Path(text_ledger)
        self.media_ledger=Path(media_ledger) if media_ledger else None
        self.originals=Path(originals) if originals else None
        self.db_path=self.root/'operations.sqlite3'
        fd=os.open(self.db_path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, envelope TEXT NOT NULL,
                mapping TEXT, response_ids TEXT, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,detail TEXT NOT NULL);''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.db_path,timeout=1)
        db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def _state(self,db,key,state,detail=None):
        now=datetime.now(timezone.utc).isoformat()
        db.execute('UPDATE operations SET state=?,updated=? WHERE id=?',(state,now,key))
        db.execute('INSERT INTO history(operation_id,state,occurred_at,detail) VALUES (?,?,?,?)',
                   (key,state,now,json.dumps(detail or {},ensure_ascii=False)))

    def accept(self,update):
        """Return True for a recognized authorized reply, after committing its envelope."""
        message=update.get('message') or {}
        if message.get('text','').strip() not in COMMANDS or not message.get('reply_to_message'):
            return False
        sender=message.get('from') or {};chat=message.get('chat') or {}
        topic=message.get('message_thread_id')
        if (str(sender.get('id'))!=self.owner_id or sender.get('is_bot') is True
                or (str(chat.get('id')),str(topic) if topic is not None else None) not in self.targets):
            return False
        uid=update.get('update_id')
        if type(uid) is not int or uid<0: raise ValueError('invalid update id')
        key=f'{self.bot_id}:{uid}'
        envelope=json.dumps(update,sort_keys=True,ensure_ascii=False)
        with self.connect() as db:
            existing=db.execute('SELECT envelope FROM operations WHERE id=?',(key,)).fetchone()
            if existing:
                if existing[0]!=envelope: raise ValueError('conflicting update id')
                return True
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                       (key,'received',envelope,None,None,datetime.now(timezone.utc).isoformat()))
            self._state(db,key,'received')
        return True

    def resolve(self,message):
        chat=message['chat']['id'];mid=message['reply_to_message']['message_id']
        rows=[r for r in read_rows(self.text_ledger) if str(r.get('chat_id'))==str(chat) and r.get('message_id')==mid
              and r.get('schema')=='sent-content.v1' and r.get('delivery_state')=='confirmed'
              and r.get('source_kind')=='telegram_channel' and public_url(r.get('source_ref'))]
        verified_rows = []
        for row in rows:
            originals = row.get('original_messages')
            if not isinstance(originals, list) or not originals:
                continue
            if (any(not isinstance(item, dict) or not isinstance(item.get('text'), str) for item in originals)
                    or [item.get('message_id') for item in originals] != row.get('source_message_ids')):
                continue
            encoded = json.dumps(originals, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if hashlib.sha256(encoded.encode()).hexdigest() != row.get('original_messages_hash'):
                continue
            text = '\n\n'.join(item['text'] for item in originals if item['text'])
            verified_rows.append({**row, 'content': text,
                                  'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                                  'mapping_provenance': 'confirmed-text-ledger+original-messages'})
        rows = verified_rows
        # Media rows only identify a URL. Require an explicitly bound original archive.
        if self.media_ledger:
            from chat_daily_tg.sent_ledger import lookup
            media=lookup(chat,mid,path=self.media_ledger)
            if media and self.originals:
                for original in read_rows(self.originals):
                    if original.get('url')==media.get('url') and original.get('producer')==media.get('producer') and original.get('verified_original') is True:
                        rows.append({**original,**media,'content':original.get('text'),
                                     'content_id':original.get('content_id'),'source_ref':original.get('source_ref'),
                                     'mapping_provenance':'media-ledger+verified-original'})
        mappings={}
        for row in rows:
            text=row.get('content');ref=row.get('source_ref')
            if not text or not ref:continue
            if row.get('content_hash') and row['content_hash']!=__import__('hashlib').sha256(text.encode()).hexdigest():
                continue
            version=digest(text)
            cid=row.get('content_id') or digest([row.get('producer'),ref,version])
            mappings[(cid,version)]={'content_id':cid,'text':text,'url':row['url'],
                'source_ref':ref,'title':row.get('title'),'producer':row.get('producer'),
                'content_hash':version,'mapping_provenance':row.get('mapping_provenance','confirmed-text-ledger')}
        return next(iter(mappings.values())) if len(mappings)==1 else None

    def process(self,key,send):
        """send(message, text) returns confirmed IDs; uncertain sends require review.

        Holding the operation transaction prevents concurrent processors. A
        persisted response_unknown precedes network I/O, so crash recovery does
        not blindly repeat a non-idempotent response.
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM operations WHERE id=?',(key,)).fetchone()
            if not row:raise ValueError('unknown operation')
            if row['state'] in {'responded','response_unknown','response_absent'}:return row['state']
            message=json.loads(row['envelope'])['message']
            mapping=json.loads(row['mapping']) if row['mapping'] else self.resolve(message)
            if not mapping:
                self._state(db,key,'needs_match',{'reason':'no unique verified original'})
                return 'needs_match'
            db.execute('UPDATE operations SET mapping=? WHERE id=?',(json.dumps(mapping,ensure_ascii=False),key))
            self._state(db,key,'matched')
            try:
                event=COMMANDS[message['text'].strip()]
                FeedbackStore(self.root/'feedback').record(event,content_id=mapping['content_id'],
                    text=mapping['text'],title=mapping.get('title'),idempotency_key=key,
                    target={'chat_id':message['chat']['id'],'message_id':message['reply_to_message']['message_id']},
                    metadata={'instruction':message['text'],'source_ref':mapping['source_ref'],
                              'url':mapping['url'],'mapping_provenance':mapping['mapping_provenance']})
                self._state(db,key,'recorded')
            except Exception as exc:
                self._state(db,key,'failed',{'error_type':type(exc).__name__})
                return 'failed'
            self._state(db,key,'response_unknown',{'reason':'send prepared; terminal receipt pending',
                'attempt_id':__import__('uuid').uuid4().hex})
        # Commit the non-retryable boundary before making an external call.
        text=(mapping['text']+'\n\n原文：'+mapping['url'] if event=='expand'
              else '已记录这条内容的“少推这类”反馈，将用于候选偏好复审。\n'+mapping['url'])
        try:
            ids=send(message,text)
            if not ids or any(type(i) is not int or i<=0 for i in ids):
                raise ValueError('missing confirmed reply receipt')
        except Exception as exc:
            with self.connect() as db:
                self._state(db,key,'response_unknown',{'error_type':type(exc).__name__})
            return 'response_unknown'
        with self.connect() as db:
            db.execute('UPDATE operations SET response_ids=? WHERE id=?',(json.dumps(ids),key))
            self._state(db,key,'responded')
        return 'responded'

    def drain(self,send):
        with self.connect() as db:
            keys=[r[0] for r in db.execute("SELECT id FROM operations WHERE state NOT IN ('responded','response_unknown','response_absent') ORDER BY updated")]
        return {key:self.process(key,send) for key in keys}

    def review_response(self, key, *, decision, actor, evidence):
        """Apply explicit reply review; only retry_requested re-enables processing."""
        if decision not in {'confirmed_sent','confirmed_absent','retry_requested'} or not actor or not isinstance(evidence,dict):
            raise ValueError('explicit reply review required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state,envelope FROM operations WHERE id=?',(key,)).fetchone()
            if not row:raise ValueError('unknown reply operation')
            target=json.loads(row['envelope'])['message']['chat']['id']
            if str(evidence.get('chat_id'))!=str(target):raise ValueError('review target mismatch')
            if decision=='retry_requested':
                if row['state']!='response_absent' or not evidence.get('reason'):
                    raise ValueError('confirm absence before requesting retry')
                state='retry_approved'
            else:
                if row['state']!='response_unknown':raise ValueError('reply is not awaiting review')
                if decision=='confirmed_sent':
                    ids=evidence.get('message_ids')
                    if (not isinstance(ids,list) or not ids or any(type(i) is not int or i<=0 for i in ids)
                            or not evidence.get('telegram_reference')):
                        raise ValueError('actual Telegram message references required')
                    db.execute('UPDATE operations SET response_ids=? WHERE id=?',(json.dumps(ids),key))
                    state='responded'
                else:
                    if not evidence.get('checked_scope'):raise ValueError('checked time/message scope required')
                    state='response_absent'
            self._state(db,key,state,{'decision':decision,'actor':actor,'evidence':evidence})
        return {'operation_id':key,'state':state}

    def summary(self):
        with self.connect() as db:
            return {r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM operations GROUP BY state')}


def configured_intake(bot_token,owner_id):
    """Optional local configuration; never creates another update consumer."""
    from chat_daily_tg.paths import STATE_DIR,SENT_CONTENT_LEDGER,MEDIA_SENT_LEDGER
    path=STATE_DIR/'content-feedback.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_uid!=os.getuid():raise ValueError('untrusted content feedback config')
    cfg=json.loads(path.read_text())
    if cfg.get('enabled') is not True:return None
    if str(cfg['owner_id'])!=str(owner_id):raise ValueError('feedback owner differs from consumer owner')
    return ReplyIntake(cfg['root'],bot_id=bot_token.split(':',1)[0],owner_id=owner_id,
                       targets=[(t['chat_id'],t.get('thread_id')) for t in cfg['targets']],
                       text_ledger=cfg.get('text_ledger',SENT_CONTENT_LEDGER),
                       media_ledger=cfg.get('media_ledger',MEDIA_SENT_LEDGER),originals=cfg.get('originals'))


def drain_configured(intake,bot_token):
    from chat_daily_tg.tg_sender import TelegramSender
    def send(message,text):
        sender=TelegramSender(bot_token,str(message['chat']['id']),message.get('message_thread_id'))
        try:return sender.send(text)
        finally:sender.close()
    return intake.drain(send)


def pending_filter_feedback(intake,candidates_root):
    used=set()
    for path in Path(candidates_root).glob('*.json'):
        try:used.update(json.loads(path.read_text()).get('feedback_ids',[]))
        except (ValueError,OSError):continue
    return [{'update_id':e['event_id'],'text':e['metadata']['instruction']+'\n主题：'+e['topic']['label']+
             '\n来源：'+e['metadata']['source_ref']+'\n内容：'+e['content']['text']}
            for e in FeedbackStore(intake.root/'feedback').iter_events()
            if e['event_type']=='filter' and e['event_id'] not in used]
