-- Fictional schema. A CREATE TABLE declares a schema in this file; it does not prove a deployed database.
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email VARCHAR(255) NOT NULL,
  nickname TEXT DEFAULT 'a;b'
);
SELECT * FROM users; -- describes a query, not its results
