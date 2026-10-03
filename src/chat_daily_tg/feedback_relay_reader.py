"""Portable read-only query for the existing BWG consumer's durable message table."""

READER_SCRIPT = r'''
import json, sqlite3, sys
from pathlib import Path
state, after, owner, raw_targets = sys.argv[1:]
targets={(str(chat), str(topic) if topic is not None else None) for chat,topic in json.loads(raw_targets)}
chats=sorted({owner, *(chat for chat,topic in targets)})
db=sqlite3.connect(Path(state).resolve().as_uri()+'?mode=ro',uri=True)
db.row_factory=sqlite3.Row
try:
    rows=db.execute('SELECT update_id,message_id,date,text,payload_json FROM messages WHERE update_id>? AND from_id=? AND chat_id IN ('+','.join('?' for _ in chats)+') ORDER BY update_id',[int(after),owner,*chats]).fetchall()
    output=[]
    for row in rows:
        message=json.loads(row['payload_json'])
        actor=message.get('from') or {};chat=message.get('chat') or {}
        if str(actor.get('id'))!=owner or actor.get('is_bot') is True:continue
        if str(chat.get('id'))==owner and chat.get('type')=='private':
            output.append({'update_id':row['update_id'],'id':row['message_id'],'date':row['date'],'text':row['text'],
                           'owner':owner,'chat_id':owner,'from_id':owner,'chat_type':'private'})
        elif (str(chat.get('id')),str(message['message_thread_id']) if message.get('message_thread_id') is not None else None) in targets:
            if message.get('reply_to_message') and message.get('text','').strip() in {'展开','少推这类'}:
                output.append({'update_id':row['update_id'],'message':message})
    print(json.dumps(output,ensure_ascii=False))
finally:
    db.close()
'''
