import json,sqlite3,subprocess,sys
from chat_daily_tg.feedback_relay_reader import READER_SCRIPT


def test_same_consumer_readonly_owner_topic_filter(tmp_path):
    path=tmp_path/'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE messages(update_id INTEGER,message_id INTEGER,date INTEGER,text TEXT,payload_json TEXT,from_id TEXT,chat_id TEXT)')
        for uid,owner,chat,topic,text in [(1,7,7,None,'preference'),(2,7,-100,2,'展开'),(3,8,-100,2,'展开'),(4,7,-100,3,'展开'),(5,7,-100,2,'unrelated')]:
            message={'message_id':uid,'from':{'id':owner},'chat':{'id':chat,'type':'private' if chat==owner else 'supergroup'},'text':text,'reply_to_message':{'message_id':99}}
            if topic is not None:message['message_thread_id']=topic
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',(uid,uid,1,text,json.dumps(message),str(owner),str(chat)))
    before=path.read_bytes()
    result=subprocess.run([sys.executable,'-c',READER_SCRIPT,str(path),'0','7',json.dumps([[-100,2]])],capture_output=True,text=True,check=True)
    rows=json.loads(result.stdout)
    assert [r['update_id'] for r in rows]==[1,2]
    assert rows[0]['text']=='preference' and rows[1]['message']['reply_to_message']['message_id']==99
    assert path.read_bytes()==before
