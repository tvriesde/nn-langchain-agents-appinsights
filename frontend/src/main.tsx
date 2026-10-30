import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { events, parseTurn, record, request, text } from "./api";
import type { Menu, Turn } from "./api";
import "./style.css";

const STORAGE_KEY = "recipe-active-conversation";
const AGENTS = ["Chef Agent", "Wine Agent", "Review Agent"];
const EXAMPLES = [
  "Salmon, asparagus, lemon and potatoes. Dinner for two.",
  "Chickpeas, spinach, tomatoes and rice. Keep it vegetarian.",
  "Mushrooms, parmesan and pasta. Something comforting.",
];

function MenuCard({ menu }: { menu: Menu }) {
  return (
    <article className="menu-card">
      <div className="eyebrow">YOUR MENU</div>
      <h3>{menu.title}</h3>
      <p>{menu.introduction}</p>
      <section>
        <h4>{menu.recipe.title} <span className="servings">Serves {menu.recipe.servings}</span></h4>
        <div className="recipe-columns">
          <div><h5>Ingredients</h5><ul>{menu.recipe.ingredients.map((item, i) => <li key={i}>{item}</li>)}</ul></div>
          <div><h5>Method</h5><ol>{menu.recipe.steps.map((item, i) => <li key={i}>{item}</li>)}</ol></div>
        </div>
        {menu.recipe.notes && <p className="muted">{menu.recipe.notes}</p>}
      </section>
      <section className="pairing"><div className="eyebrow">THE PAIRING</div>
        <h4>{menu.pairing.wine}</h4><p>{menu.pairing.rationale}</p>
        <p className="muted">{menu.pairing.serving_tip}</p>
      </section>
      {menu.review_notes && <p className="review"><strong>Reviewer&apos;s note.</strong> {menu.review_notes}</p>}
    </article>
  );
}

function App() {
  const [conversation, setConversation] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [continuation, setContinuation] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeAgent, setActiveAgent] = useState<string | null>(null);
  const [completedAgents, setCompletedAgents] = useState<string[]>([]);
  const bottom = useRef<HTMLDivElement>(null);

  function showError(cause: unknown) {
    setError(cause instanceof Error ? cause.message : "Something went wrong. Refresh and try again.");
  }

  async function loadHistory(id: string, token?: string) {
    const url = `/api/conversations/${id}${token ? `?continuation=${encodeURIComponent(token)}` : ""}`;
    const data = record(await (await request(url)).json());
    if (!Array.isArray(data.turns)) throw new Error("Invalid conversation history.");
    const loaded = data.turns.map(parseTurn).reverse();
    setTurns((current) => token ? [...loaded, ...current] : loaded);
    setContinuation(data.continuation ? text(data.continuation) : null);
  }

  async function newConversation() {
    const data = record(await (await request("/api/conversations", { method: "POST" })).json());
    const id = text(data.id);
    localStorage.setItem(STORAGE_KEY, id);
    setConversation(id);
    setTurns([]);
    setContinuation(null);
    setActiveAgent(null);
    setCompletedAgents([]);
    setError(null);
  }

  useEffect(() => {
    async function initialize() {
      try {
        const id = localStorage.getItem(STORAGE_KEY);
        if (id) {
          setConversation(id);
          await loadHistory(id);
        } else {
          await newConversation();
        }
      } catch (cause) {
        showError(cause);
      } finally {
        setBusy(false);
      }
    }
    void initialize();
  }, []);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [turns, activeAgent]);

  async function action(run: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try { await run(); } catch (cause) { showError(cause); } finally { setBusy(false); }
  }

  async function removeConversation() {
    if (!conversation || !window.confirm(
      "Delete this conversation from Cosmos DB? Copies in Application Insights follow its separate retention policy.",
    )) return;
    await action(async () => {
      await request(`/api/conversations/${conversation}`, { method: "DELETE" });
      localStorage.removeItem(STORAGE_KEY);
      setConversation(null);
      setTurns([]);
      await newConversation();
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!conversation || !input.trim() || busy) return;
    const message = input.trim();
    const turnId = crypto.randomUUID();
    setInput("");
    setBusy(true);
    setError(null);
    setActiveAgent(null);
    setCompletedAgents([]);
    const pending: Turn = {
      id: turnId, message, status: "pending", trace_id: "", menu: null, error: null, context_truncated: false,
    };
    setTurns((current) => [...current, pending]);
    let terminal = false;
    try {
      const response = await request(`/api/conversations/${conversation}/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, turn_id: turnId }),
      });
      for await (const item of events(response)) {
        if (item.event === "turn_started") {
          setTurns((current) => current.map((turn) => turn.id === turnId ? {
            ...turn, trace_id: text(item.data.trace_id), context_truncated: item.data.context_truncated === true,
          } : turn));
        } else if (item.event === "agent_started") {
          setActiveAgent(text(item.data.agent));
        } else if (item.event === "agent_completed") {
          setCompletedAgents((current) => [...current, text(item.data.agent)]);
        } else if (item.event === "completed" || item.event === "error") {
          const result = parseTurn(item.data);
          setTurns((current) => current.map((turn) => turn.id === turnId ? result : turn));
          terminal = true;
          if (item.event === "error") setError(result.error);
        }
      }
      if (!terminal) throw new Error("Connection interrupted. Refresh history before submitting a new request.");
    } catch (cause) {
      showError(cause);
      setTurns((current) => current.map((turn) => turn.id === turnId && turn.status === "pending"
        ? { ...turn, status: "failed", error: "Could not confirm completion. Refresh history." } : turn));
    } finally {
      setBusy(false);
      setActiveAgent(null);
    }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="The Agent's Table home"><span className="brand-mark">AT</span>
          <span>The Agent&apos;s Table<small>A SMALL MULTI-AGENT KITCHEN</small></span></a>
        <div className="header-actions">
          <button className="quiet" disabled={busy || !conversation} onClick={() => void action(
            () => loadHistory(conversation!),
          )}>Refresh history</button>
          <button className="quiet danger" disabled={busy || !conversation} onClick={() => void removeConversation()}>Delete chat</button>
          <button className="secondary" disabled={busy} title="Starts a new conversation without deleting the current one"
            onClick={() => void action(newConversation)}>New chat</button>
        </div>
      </header>
      <main>
        <section className="intro">
          <div className="eyebrow">INGREDIENTS IN. INSPIRATION OUT.</div>
          <h1>Good food.<br /><span>Thoughtful company.</span></h1>
          <p>Tell us what&apos;s in your kitchen. A chef creates the recipe, a sommelier finds
            the pairing, and a reviewer brings it all together.</p>
        </section>
        <section className="workspace">
          <aside className="team">
            <div className="eyebrow">YOUR THREE SPECIALISTS</div>
            {AGENTS.map((name, i) => (
              <div className={`agent ${activeAgent === name ? "working" : ""}`} key={name}>
                <span className="agent-number">{completedAgents.includes(name) ? "\u2713" : `0${i + 1}`}</span>
                <div><h3>{name.replace(" Agent", "")}</h3><p>{[
                  "Turns ingredients into a recipe.", "Finds a harmonious pairing.", "Checks and presents your menu.",
                ][i]}</p>{activeAgent === name && <small className="working-label">Working...</small>}</div>
              </div>
            ))}
            <div className="architecture"><span>React + FastAPI</span><span>LangChain / Azure OpenAI</span>
              <span>Cosmos DB / Application Insights</span></div>
            <p className="privacy">Public demonstration. Messages and agent outputs are stored in Cosmos DB
              and telemetry. Do not enter sensitive information.</p>
            <p className="privacy">New chat keeps old data. Delete this chat first if you want to remove it.
              Telemetry has separate retention.</p>
          </aside>
          <section className="chat" aria-label="Recipe conversation">
            <div className="chat-heading"><h2>At the table</h2><span className="badge">3 agents, one menu</span></div>
            {error && <div role="alert" className="error">{error}</div>}
            {continuation && <button className="quiet load-more" disabled={busy}
              onClick={() => void action(() => loadHistory(conversation!, continuation))}>Load earlier messages</button>}
            <div className="messages" aria-live="polite" aria-busy={busy}>
              {turns.length === 0 && <div className="empty">
                <div className="plate" aria-hidden="true"><span>AT</span></div>
                <h3>What are we cooking?</h3><p>Start with a few ingredients, servings, or a craving.</p>
                <div className="examples">{EXAMPLES.map((example) =>
                  <button disabled={busy} key={example} onClick={() => setInput(example)}>{example}</button>)}</div>
              </div>}
              {turns.map((turn) => <div className="turn" key={turn.id}>
                <div className="user-message"><div className="eyebrow">YOU</div><p>{turn.message}</p></div>
                {turn.menu && <MenuCard menu={turn.menu} />}
                {turn.status === "pending" && <p className="pending">
                  {activeAgent ? `${activeAgent} is working...` : busy ? "Preparing your request..." :
                    "This turn is still pending. Refresh history; interrupted turns recover after about four minutes."}</p>}
                {turn.error && <p className="error">{turn.error}</p>}
                {turn.context_truncated && <p className="muted small">Older turns were omitted from model context; stored history is unchanged.</p>}
                {turn.trace_id && <div className="trace">TRACE <code>{turn.trace_id}</code></div>}
              </div>)}
              <div ref={bottom} />
            </div>
            <form className="composer" onSubmit={(event) => void submit(event)}>
              <label htmlFor="ingredients">Your ingredients or follow-up</label>
              <textarea id="ingredients" rows={3} maxLength={4000} value={input} disabled={busy || !conversation}
                placeholder="Chicken, lemon, garlic, a handful of herbs..."
                onChange={(event) => setInput(event.target.value)} />
              <div className="composer-bottom"><small>{input.length}/4000 &middot; Saved in Cosmos DB &middot; Same-browser access</small>
                <button className="primary" type="submit" disabled={busy || !conversation || !input.trim()}>
                  {busy ? "Working..." : turns.length ? "Refine the menu" : "Create my menu"} <span aria-hidden="true">&rarr;</span>
                </button></div>
            </form>
          </section>
        </section>
      </main>
      <footer>Built to make the handoffs visible. Open a trace in Application Insights to follow the whole kitchen.</footer>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
