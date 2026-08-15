// Blokuje pobieranie DOKUMENTACJI przez `curl`/`wget` w narzedziu `bash`.
//
// POWSTAL PO KONKRETNEJ WPADCE (2026-08-15). Regula w `~/.omp/agent/RULES.md`
// mowi: "Nigdy nie obchodzic strony renderowanej JS-em kolejnymi wariantami
// `curl`". Ta regula jest sticky — doklejana przy biezacej turze — i mimo to
// zostala zlamana trzy razy z rzedu na `mariadb.org`, za kazdym razem innym
// regexem, zanim uzyty zostal `read`. Tekst instrukcji nie wystarczyl, wiec to
// jest ta sama zasada wyrazona jako bramka.
//
// CZEGO NIE BLOKUJE — wazniejsze niz to, co blokuje:
// API, rejestry i repozytoria pakietow sa legalnym celem `curl`. Ten sam
// przebieg, ktory zle zeskrobal `mariadb.org/about/`, poprawnie odpytal
// `downloads.mariadb.org/rest-api/`, `registry-1.docker.io/v2/` i
// `repo.percona.com/.../RPMS/`. Bramka blokujaca jedno i drugie zostalaby
// wylaczona po tygodniu, wiec allowlista maszynowa ma PIERWSZENSTWO przed
// detekcja dokumentacji.
//
// Katalog `.omp/` w repo tworzy ten plik. Native discovery czyta
// `AGENTS.md`/`RULES.md` tylko z najblizszego niepustego `.omp/`, ale zaden z
// tych plikow tu nie istnieje, a `RULES.md` uzytkownika i tak przeslonilby
// projektowy. Zakres zmiany to wylacznie ten hook.

/** Minimalny ksztalt kontraktu hooka — celowo bez importu z pakietu omp, zeby
 *  plik dal sie odpalic i przetestowac samym `deno`, bez toolchainu w repo. */
interface ToolCallEvent {
  toolName: string;
  input: Record<string, unknown>;
}
interface HookApiLike {
  on(
    event: "tool_call",
    handler: (
      event: ToolCallEvent,
    ) => Promise<{ block?: boolean; reason?: string } | undefined>,
  ): void;
}

export interface Verdict {
  block: boolean;
  url?: string;
  reason?: string;
}

// --- Sygnaly "to sa dane maszynowe" (sprawdzane pierwsze) --------------------
const MACHINE_HOST =
  /^(api|registry[\w.-]*|auth|repo|repos|downloads|raw|objects|deb|yum|archive)\./i;
const MACHINE_HOST_EXACT: Record<string, true> = {
  "registry-1.docker.io": true,
  "auth.docker.io": true,
  "index.docker.io": true,
  "api.github.com": true,
  "raw.githubusercontent.com": true,
  "objects.githubusercontent.com": true,
};
const MACHINE_PATH =
  /(\/rest-api\/|\/api\/|\/v\d+\/|\/rpms?\/|\/repodata\/|\/dists\/|\/pool\/|\/releases\/download\/|\/manifests\/|\/blobs\/|\/tags(\?|$))/i;
const MACHINE_EXT =
  /\.(json|ya?ml|xml|txt|rpm|deb|tgz|tar\.gz|tar\.xz|zip|asc|sig|sha\d+|sum|lock|hcl|repo)(\?|$)/i;

// --- Sygnaly "to jest dokumentacja do czytania" ------------------------------
const DOC_HOST_PREFIX =
  /^(docs?|documentation|wiki|kb|learn|devcenter|developer)\./i;
const DOC_HOST_EXACT: Record<string, true> = {
  "mariadb.org": true,
  "mariadb.com": true,
  "galeracluster.com": true,
  "proxysql.com": true,
  "readthedocs.io": true,
  "kubernetes.io": true,
  "developer.mozilla.org": true,
};
const DOC_PATH =
  /(\/docs?\/|\/documentation\/|\/manual\/|\/kb\/|\/knowledge-?base\/|\/guide\/|\/reference\/|\/release-notes?\/|\/about\/|\/library\/)/i;

/** Zdejmowanie tagow HTML w tym samym poleceniu. Zaostrza komunikat, ale NIE
 *  tworzy werdyktu samo — `grep` po listingu katalogu repozytorium tez trafia
 *  w HTML i jest calkowicie w porzadku. */
const HTML_STRIPPING =
  /(<\[\^>\]|<\[\^<>\]|re\.(sub|findall|finditer|search)\s*\(\s*r?['"][^'"]*<|s\/<\[\^>\]\*>|html\.unescape|BeautifulSoup|\bpup\b|hxselect|xmllint\s+--html)/i;

const FETCHER = /\b(curl|wget|xh)\b/;
const URL_IN_COMMAND = /https?:\/\/[^\s"'`)>|\\]+/gi;

/** Czysta funkcja decyzyjna — testowalna bez uruchamiania harnessu. */
export function inspectCommand(command: string): Verdict {
  if (!FETCHER.test(command)) return { block: false };

  for (const match of command.matchAll(URL_IN_COMMAND)) {
    const raw = match[0];
    let parsed: URL;
    try {
      parsed = new URL(raw);
    } catch {
      continue;
    }
    const host = parsed.hostname.toLowerCase();

    if (MACHINE_HOST_EXACT[host]) continue;
    if (MACHINE_HOST.test(host)) continue;
    if (MACHINE_PATH.test(parsed.pathname + parsed.search)) continue;
    if (MACHINE_EXT.test(parsed.pathname)) continue;

    const docHost =
      DOC_HOST_PREFIX.test(host) ||
      DOC_HOST_EXACT[host] === true ||
      Object.keys(DOC_HOST_EXACT).some((known) => host.endsWith("." + known));
    if (!docHost && !DOC_PATH.test(parsed.pathname)) continue;

    return {
      block: true,
      url: raw,
      reason:
        `Pobieranie dokumentacji przez curl/wget jest zablokowane: ${raw}\n` +
        (HTML_STRIPPING.test(command)
          ? "Polecenie dodatkowo zdejmuje tagi HTML — to dokladnie ten wzorzec, " +
            "ktory reguly nazywaja i ktory zwraca menu nawigacyjne zamiast tresci.\n"
          : "") +
        "Kolejnosc wg RULES.md: Context7 (mcp__context_query_docs) -> " +
        `read('${raw}') -> API projektu -> scraping na samym koncu.\n` +
        "`read` robi negocjacje tresci i ekstrakcje reader-mode, wiec zwraca " +
        "tabele i tresc, ktorych regex po surowym HTML nie znajduje.",
    };
  }
  return { block: false };
}

export default function hook(pi: HookApiLike): void {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return undefined;
    const verdict = inspectCommand(String(event.input.command ?? ""));
    return verdict.block ? { block: true, reason: verdict.reason } : undefined;
  });
}
