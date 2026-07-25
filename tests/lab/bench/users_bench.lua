-- sysbench workload dla realnej tabeli isa_test.users (nie sbtest).
-- Cały ruch idzie przez endpoint ProxySQL (VIP), czyli ścieżką aplikacji:
-- R/W split jest wyłączony (ISC-23), więc wszystko trafia na jednego writera.
--
-- Tryby (--mode):
--   point  — SELECT po kluczu głównym (PRIMARY)
--   email  — SELECT po unikalnym indeksie wtórnym (uk_email)
--   range  — agregat po indeksie złożonym (idx_status_created)
--   update — UPDATE (ścieżka zapisu → replikacja Galera do 3 węzłów)
--   mixed  — 80% odczytu po PK / 20% zapisu (profil zbliżony do aplikacji)
--
-- Uruchomienie:
--   sysbench --db-driver=mysql --mysql-host=<vip> --mysql-port=6033 \
--            --mysql-user=app_user --mysql-db=isa_test \
--            users_bench.lua --mode=point --time=20 --threads=8 run

sysbench.cmdline.options = {
   mode  = {"tryb: point|email|range|update|mixed", "mixed"},
   users = {"liczba wierszy w tabeli users", 200000}
}

function thread_init()
   drv = sysbench.sql.driver()
   con = drv:connect()
end

function thread_done()
   con:disconnect()
end

local function rand_id()
   return sysbench.rand.uniform(1, sysbench.opt.users)
end

local function q_point()
   con:query(string.format(
      "SELECT id, username, email, status, login_count FROM users WHERE id = %d", rand_id()))
end

local function q_email()
   con:query(string.format(
      "SELECT id, username, status FROM users WHERE email = 'user%d@example.test'", rand_id()))
end

local function q_range()
   -- agregat po idx_status_created; losowe okno czasowe, żeby nie trafiać wciąż w cache
   local days = sysbench.rand.uniform(7, 365)
   local st = ({"active", "suspended", "deleted"})[sysbench.rand.uniform(1, 3)]
   con:query(string.format(
      "SELECT COUNT(*), AVG(login_count) FROM users " ..
      "WHERE status = '%s' AND created_at > NOW() - INTERVAL %d DAY", st, days))
end

local function q_update()
   -- zapis replikowany przez Galerę do wszystkich węzłów (certyfikacja writesetu)
   con:query(string.format(
      "UPDATE users SET login_count = login_count + 1, last_login_at = NOW() WHERE id = %d",
      rand_id()))
end

function event()
   local m = sysbench.opt.mode
   if m == "point" then
      q_point()
   elseif m == "email" then
      q_email()
   elseif m == "range" then
      q_range()
   elseif m == "update" then
      q_update()
   else -- mixed: 80/20
      if sysbench.rand.uniform(1, 100) <= 80 then q_point() else q_update() end
   end
end
