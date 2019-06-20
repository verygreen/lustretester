create table known_crashes (id serial unique, reason varchar(120) NOT NULL, func varchar(50), testline varchar(120), backtrace text NOT NULL, inlogs varchar(200), infullbt varchar(200), bug varchar(20) NOT NULL, extrainfo text);
create table new_crashes (id serial PRIMARY KEY, reason varchar(250) NOT NULL, func varchar(50), backtrace text NOT NULL); -- I wanter primary key on reason,func,backtrace but it does not work with select group by
create table triage (id serial PRIMARY KEY, link varchar(200) NOT NULL, testline varchar(120), fullcrash text NOT NULL, testlogs text, newcrash_id integer not null references new_crashes (id) ON DELETE CASCADE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), FOREIGN KEY (newcrash_id) references new_crashes (id)); -- blob with entire compressed dmesg?
create index on known_crashes (reason, func, testline);
create index on new_crashes (reason, func);

# For failure info
create table failures (id serial unique, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), branch varchar(30), GerritID integer, test varchar(50), subtest varchar(50), fstype varchar(20), duration integer DEFAULT 0, error text, Link text);
CREATE TABLE warnings (id serial unique, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), branch varchar(30), GerritID integer, test varchar(50), warning text, fstype varchar(20), Link text);
create index on failures (GerritID);
create index on failures (test, subtest, fstype, error, branch);
create index on failures (created_at);

create table blacklisted (id serial unique, test varchar(50), subtest varchar(50), fstype varchar(20), errorstart text);
