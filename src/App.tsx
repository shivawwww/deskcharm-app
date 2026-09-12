import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { createRope, stepRope, type RopePoint } from "./useRope";
import { DEFAULT_CHARMS, ritualFor, type Charm, type RitualType } from "./charms";
import { playRitualSound } from "./sound";
import { CharmGlyph } from "./charmArt";
import "./App.css";

const MAX_TILT_DEG = 22;

const ANCHOR_Y = 8;
const CHARM_INDEX = 6;
const MARGIN = 26;

function loadCharm(): Charm {
  try {
    const saved = localStorage.getItem("deskcharm.charm");
    if (saved) return JSON.parse(saved);
  } catch {
    // ignore corrupt storage
  }
  return DEFAULT_CHARMS[0];
}

export default function App() {
  const [stage, setStage] = useState<{ width: number; height: number } | null>(null);
  const [anchorX, setAnchorX] = useState(400);
  const [charm, setCharm] = useState<Charm>(loadCharm);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPos, setMenuPos] = useState({ x: 400, y: 120 });
  const [customEmoji, setCustomEmoji] = useState("");
  const [activeRitual, setActiveRitual] = useState<RitualType | null>(null);
  const [charmPos, setCharmPos] = useState({ x: 400, y: ANCHOR_Y + CHARM_INDEX * 16 });
  const [tilt, setTilt] = useState(0);
  const [lean, setLean] = useState({ x: 0, y: 0 });

  const pointsRef = useRef<RopePoint[]>(createRope(anchorX, ANCHOR_Y));
  const anchorXRef = useRef(anchorX);
  const dragIndexRef = useRef<number | null>(null);
  const dragPosRef = useRef<{ x: number; y: number } | null>(null);
  const anchorDraggingRef = useRef(false);
  const downRef = useRef<{ x: number; y: number } | null>(null);
  const timeRef = useRef(0);
  const frameCountRef = useRef(0);
  const menuOpenRef = useRef(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    invoke<[number, number]>("get_stage_size").then(([w, h]) => {
      const x = w / 2;
      anchorXRef.current = x;
      setAnchorX(x);
      pointsRef.current = createRope(x, ANCHOR_Y);
      setStage({ width: w, height: h });
    });
  }, []);

  useEffect(() => {
    localStorage.setItem("deskcharm.charm", JSON.stringify(charm));
  }, [charm]);

  useEffect(() => {
    if (!stage) return;
    let raf = 0;
    const bounds = { width: stage.width, height: stage.height, margin: MARGIN };
    const tick = () => {
      timeRef.current += 1;
      frameCountRef.current += 1;
      const wind = Math.sin(timeRef.current * 0.02) * 0.06;
      stepRope(pointsRef.current, anchorXRef.current, ANCHOR_Y, wind, dragIndexRef.current, dragPosRef.current, bounds);
      const tip = pointsRef.current[CHARM_INDEX];
      setCharmPos({ x: tip.x, y: tip.y });

      const velocityX = tip.x - tip.px;
      const swingTilt = Math.max(-MAX_TILT_DEG, Math.min(MAX_TILT_DEG, velocityX * 3.2));
      setTilt(swingTilt);

      if (frameCountRef.current % 2 === 0) {
        const rects: [number, number, number, number][] = [];
        if (menuOpenRef.current && menuRef.current) {
          const r = menuRef.current.getBoundingClientRect();
          rects.push([r.x, r.y, r.width, r.height]);
        }
        invoke("update_hit_points", {
          points: [
            [tip.x, tip.y],
            [anchorXRef.current, ANCHOR_Y],
          ],
          rects,
        }).catch(() => {});
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [stage]);

  useEffect(() => {
    const unlisten = listen("recenter", () => {
      if (!stage) return;
      const x = stage.width / 2;
      anchorXRef.current = x;
      setAnchorX(x);
    });
    return () => {
      unlisten.then((f) => f());
    };
  }, [stage]);

  const setForceInteractive = (active: boolean) => {
    invoke("set_force_interactive", { active }).catch(() => {});
  };

  const closeMenu = () => {
    menuOpenRef.current = false;
    setMenuOpen(false);
    setForceInteractive(false);
  };

  useEffect(() => {
    menuOpenRef.current = menuOpen;
    if (menuOpen) setForceInteractive(true);
  }, [menuOpen]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeMenu();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  const onCharmPointerDown = (e: React.PointerEvent) => {
    e.stopPropagation();
    if (e.button !== 0) return;
    (e.target as Element).setPointerCapture(e.pointerId);
    dragIndexRef.current = CHARM_INDEX;
    dragPosRef.current = { x: e.clientX, y: e.clientY };
    downRef.current = { x: e.clientX, y: e.clientY };
    if (menuOpenRef.current) {
      menuOpenRef.current = false;
      setMenuOpen(false);
    }
    setForceInteractive(true);
  };

  const onCharmPointerMove = (e: React.PointerEvent) => {
    if (dragIndexRef.current !== null) {
      dragPosRef.current = { x: e.clientX, y: e.clientY };
      return;
    }
    const dx = e.clientX - charmPos.x;
    const dy = e.clientY - charmPos.y;
    const dist = Math.hypot(dx, dy) || 1;
    const pull = Math.min(dist / 60, 1) * 7;
    setLean({ x: -(dx / dist) * pull, y: -(dy / dist) * pull * 0.4 });
  };

  const onCharmPointerLeave = () => {
    setLean({ x: 0, y: 0 });
  };

  const triggerRitual = (ritual: RitualType) => {
    setActiveRitual(ritual);
    playRitualSound(ritual);
    setTimeout(() => setActiveRitual(null), 900);
  };

  const onCharmPointerUp = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    dragIndexRef.current = null;
    dragPosRef.current = null;
    if (!menuOpenRef.current) setForceInteractive(false);
    const down = downRef.current;
    if (down) {
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
      if (moved < 4) {
        triggerRitual(ritualFor(charm));
      }
    }
    downRef.current = null;
  };

  const onCharmContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setMenuOpen((open) => {
      const next = !open;
      menuOpenRef.current = next;
      if (next) {
        setMenuPos({ x: charmPos.x, y: charmPos.y });
        setForceInteractive(true);
      } else {
        setForceInteractive(false);
      }
      return next;
    });
  };

  const onAnchorPointerDown = (e: React.PointerEvent) => {
    e.stopPropagation();
    (e.target as Element).setPointerCapture(e.pointerId);
    anchorDraggingRef.current = true;
    setForceInteractive(true);
  };

  const onAnchorPointerMove = (e: React.PointerEvent) => {
    if (!anchorDraggingRef.current || !stage) return;
    const x = Math.min(Math.max(e.clientX, MARGIN), stage.width - MARGIN);
    anchorXRef.current = x;
    setAnchorX(x);
  };

  const onAnchorPointerUp = () => {
    anchorDraggingRef.current = false;
    if (!menuOpenRef.current) setForceInteractive(false);
  };

  const chooseCharm = (c: Charm) => {
    setCharm(c);
    closeMenu();
  };

  const applyCustomEmoji = () => {
    const trimmed = customEmoji.trim();
    if (!trimmed) return;
    setCharm({
      id: "custom",
      emoji: trimmed,
      name: "Custom",
      ritual: "sparkle",
      region: "Your own",
      description: "A charm of your own choosing, hung on the same thread as the rest.",
      actionLabel: "Give it a shake",
    });
    setCustomEmoji("");
    closeMenu();
  };

  if (!stage) return null;

  const rope = pointsRef.current;
  const points = rope.map((p) => `${p.x},${p.y}`).join(" ");

  return (
    <div
      className="stage"
      style={{ width: stage.width, height: stage.height }}
      onPointerDown={() => menuOpen && closeMenu()}
    >
      <svg className="thread" width={stage.width} height={stage.height}>
        <polyline points={points} fill="none" stroke="rgba(20,16,12,0.55)" strokeWidth={3.2} strokeLinecap="round" />
        <polyline points={points} fill="none" stroke="rgba(255,250,240,0.85)" strokeWidth={1.1} strokeLinecap="round" />
      </svg>

      <div
        className="anchor-handle"
        style={{ left: anchorX, top: ANCHOR_Y }}
        onPointerDown={onAnchorPointerDown}
        onPointerMove={onAnchorPointerMove}
        onPointerUp={onAnchorPointerUp}
        title="Drag to move along the top"
      />

      {activeRitual === "sparkle" && (
        <div className="sparkle-burst" style={{ left: charmPos.x, top: charmPos.y }}>
          {Array.from({ length: 6 }).map((_, i) => (
            <span key={i} className="spark" style={{ "--i": i } as React.CSSProperties} />
          ))}
        </div>
      )}
      {activeRitual === "chime" && (
        <div className="chime-rings" style={{ left: charmPos.x, top: charmPos.y }}>
          <span className="ring" />
          <span className="ring ring-delay" />
        </div>
      )}

      <div
        data-charm
        className="charm"
        style={{
          left: charmPos.x,
          top: charmPos.y,
          transform: `translate(-50%, -50%) translate(${lean.x}px, ${lean.y}px) rotateZ(${(tilt + lean.x * 0.6).toFixed(2)}deg) rotateY(${(tilt * 1.3).toFixed(2)}deg)`,
        }}
        onPointerDown={onCharmPointerDown}
        onPointerMove={onCharmPointerMove}
        onPointerUp={onCharmPointerUp}
        onPointerLeave={onCharmPointerLeave}
        onContextMenu={onCharmContextMenu}
        title={`${charm.name} — click for a ritual, right-click to change`}
      >
        <span className={`charm-inner ${activeRitual ? `ritual-${activeRitual}` : "idle"}`}>
          <CharmGlyph charm={charm} size={40} />
        </span>
      </div>

      {menuOpen && <div className="menu-backdrop" onPointerDown={closeMenu} />}

      {menuOpen && (
        <div
          ref={menuRef}
          className="menu"
          style={{
            left: Math.min(Math.max(menuPos.x - 145, 12), stage.width - 302),
            top: Math.min(menuPos.y + 34, Math.max(12, stage.height - 280)),
            maxHeight: Math.max(240, stage.height - Math.min(menuPos.y + 34, Math.max(12, stage.height - 280)) - 16),
          }}
          onPointerDown={(e) => e.stopPropagation()}
          onWheel={(e) => e.stopPropagation()}
        >
          <div className="menu-arrow" style={{ left: Math.min(133, menuPos.x - Math.max(menuPos.x - 145, 12) - 8) }} />
          <div className="menu-header">
            <p className="menu-label">choose a charm</p>
            <button type="button" className="menu-close" onClick={closeMenu} aria-label="Close">
              Close
            </button>
          </div>
          <div className="roster">
            {DEFAULT_CHARMS.map((c) => (
              <button
                type="button"
                key={c.id}
                className={`roster-card ${c.id === charm.id ? "active" : ""}`}
                onPointerDown={(e) => e.stopPropagation()}
                onClick={() => chooseCharm(c)}
              >
                <span className="roster-glyph">
                  <span className="roster-cord" />
                  <span className="roster-bead" />
                  <span className="roster-emoji">
                    <CharmGlyph charm={c} size={30} />
                  </span>
                </span>
                <span className="roster-name">{c.name}</span>
                <span className="roster-tag">{c.region}</span>
                <span className="roster-desc">{c.description}</span>
                <span className="roster-action">{c.actionLabel}</span>
              </button>
            ))}
          </div>
          <div className="menu-footer">
            <div className="menu-divider" />
            <p className="menu-label">or type your own</p>
            <div className="menu-custom">
              <input
                value={customEmoji}
                placeholder="😀"
                maxLength={4}
                onChange={(e) => setCustomEmoji(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && applyCustomEmoji()}
              />
              <button type="button" className="menu-set" onClick={applyCustomEmoji}>
                Set
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
