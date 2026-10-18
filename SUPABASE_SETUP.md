# Supabase / PostgreSQL: environment variable ছাড়া

Build: `desi-2026.10.18-supabase / <source hash>`।

## যা এখন কাজ করে

- Native `psycopg` connection; Telegram IDs/access hashes BIGINT, সময় DOUBLE PRECISION।
- নিজস্ব `bfixf` schema। Supabase-এর existing public/auth/storage tables পরিবর্তন হয় না।
- Existing SQL-এর conflict/update, pagination, campaign queue, date analytics ও visitor queries।
- Consistent PostgreSQL snapshot → পুরোনো SQLite-ভিত্তিক `.bfixf.gz` backup format।
- SQLite/portable backup → transactional PostgreSQL import; IDs, links, hashes, settings,
  sessions ও counters বজায় থাকে। Restore ব্যর্থ হলে transaction rollback। Import-এর পরে
  sequence ঠিক হয় যাতে পরের upload-এ পুরোনো ID-র সঙ্গে সংঘর্ষ না হয়।
- Startup-এ configured local SQLite থাকলে **শুধু empty PostgreSQL destination-এ** copy হয়।
  Source file মুছে না। Destination-এ ডেটা থাকলে automatic merge/overwrite হয় না।
- PostgreSQL unavailable থাকলে normal bot/application writes শুরু হয় না। Website recovery
  mode-এ `/admin/login` → `/setup` থাকে; ১০ সেকেন্ড অন্তর reconnect চেষ্টাও চলে। এটি দ্বিতীয়
  SQLite production database-এ failover করে আলাদা ডেটা তৈরি করে না।

## আপনার private configuration

Private configuration-এর file name `database.private.json`। সেটি public repository বা
public container image-এ দেবেন না। File-এ provider connection URL ও `sslmode` থাকে।
এই project-এ file git-ignored; credential public source-এ hardcode করা হয় না।

### Render-এর Git-based deployment

1. Branch `arena/182861c6-bfixf` deploy করুন; Python 3, build `pip install -r requirements.txt`,
   start `python main.py`। Bot credential environment-এ দেওয়ার প্রয়োজন নেই।
2. `/admin/login` দিয়ে login → `/setup` → **Supabase / PostgreSQL** card-এ private JSON
   upload করে confirm করুন। URL/API values আলাদা করে টাইপ করতে হবে না।
3. Site বাস্তব connection যাচাই করবে। নতুন/offline unconfigured install হলে তখনই যুক্ত হবে।
   চলমান bot/session থাকলে config save হবে; data source বদলানোর জন্য service restart করুন।
4. পুরোনো local SQLite থাকলে empty destination-এ startup import হয়। শুধু backup থাকলে
   `/setup` restore form বা `/admin/backup` থেকে upload করুন। পুরোনো host-এ একই bot/DB
   writing বন্ধ রাখুন; cross-host concurrent writes চললে migration-কে zero-loss বলা যায় না।

### ZIP / container / VPS deployment

Private code ZIP-এর root-এ configuration file আছে। Code unzip → dependencies install →
`python main.py`। Environment variable দিতে হবে না। ZIP-এ পুরোনো production database নেই;
নিজের পুরোনো DB রেখে দিন বা backup restore করুন। একই token দুই host-এ চালাবেন না।

### Config file স্থায়ী রাখা

`/var/data` writable disk mount থাকলে private config সেখান থেকে নেওয়া/সেভ করা হয়;
না থাকলে project root। Render Git deployment-এ git-ignored file যায় না—website upload
সেই জন্য আছে। Persistent disk ছাড়া redeploy-এ private connection file হারাতে পারে।
তাতে Supabase-এর ডেটা মুছে যায় না, কিন্তু JSON আবার upload করতে হবে। Browser থেকে
এগুলো দেওয়া মানে কোনো environment variable দরকার নেই; private bootstrap configuration
কোথাও durable ভাবে রাখা দরকার, এই শর্ত বদলায় না।

## Verification / testing

- Source hash ও build: bot `/version`। Website backup page-এ backend/hostname দেখায়,
  password/connection URL দেখায় না।
- Backup export করুন; test restore-এ IDs ও access hashes মিলিয়ে নিন।
- Local PostgreSQL-compatible engine-এ protocol/SQL tests চলেছে; production Supabase
  authentication, provider permissions, firewall ও latency এখানে যাচাই করা যায়নি।
- Disposable test database থাকলে:
  `python -m pytest tests/test_postgres.py --postgres-test-url='postgresql://…'`
  **এই option production project-এ ব্যবহার করবেন না; test bfixf schema খালি করে।**
- Ordinary suite: `python -m pytest -q`। PostgreSQL integration tests URL না দিলে skip হয়।

## pompom থেকে নেওয়া আপডেট

Reference commit `b969bc09`: account-specific channel access-hash handling, archived
folder resolution, stale-hash retry, বড় free-video pool থেকে group selection,
short-lived cooldown notice, friendly/coalesced private error alerts। পুরো repository
অন্ধভাবে replace করা হয়নি; database ও published IDs অপরিবর্তিত রেখে নির্দিষ্ট diff নেওয়া হয়েছে।

ছোট connection ZIP-এর root-এ `database.private.json` থাকলে সেটিও সরাসরি upload করা যায়; extract করতে হয় না। ZIP ও JSON সর্বোচ্চ 16 KB। Application source ZIP এই upload field-এর জন্য নয়।
