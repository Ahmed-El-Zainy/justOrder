// Provisions the runtime application user with the `read` role ONLY.
//
// This is the first of the two independent controls required by constitution
// Principle II. The second is the aggregation stage allow-list in
// backend/app/agent/guards.py. Neither may be removed on the grounds that the
// other exists: the credential stops writes the validator might somehow miss,
// and the validator stops $where/$function code execution that a read-only
// credential would happily permit.
//
// Runs once, on first container start, against an empty data directory.

const dbName = "procurement";
const appUser = process.env.MONGO_APP_USER || "procurement_app";
const appPassword = process.env.MONGO_APP_PASSWORD || "procurement_app_pw";

const target = db.getSiblingDB(dbName);

target.createUser({
  user: appUser,
  pwd: appPassword,
  roles: [{ role: "read", db: dbName }],
});

print(`[mongo-init] created read-only user '${appUser}' on '${dbName}'`);

// Create the collections up front so a read-only user can query them before the
// loader has run. Without this, the app's first query fails with a namespace
// error rather than returning an honest empty result.
target.createCollection("purchase_orders");
target.createCollection("field_vocabulary");

print("[mongo-init] collections ready");
