use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, WebviewWindow};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

#[cfg(target_os = "windows")]
use windows::Win32::Foundation::POINT;
#[cfg(target_os = "windows")]
use windows::Win32::UI::WindowsAndMessaging::GetCursorPos;

#[cfg(target_os = "macos")]
use core_graphics::event::CGEvent;
#[cfg(target_os = "macos")]
use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};

const HIT_RADIUS_ENTER: f64 = 42.0;
const HIT_RADIUS_EXIT: f64 = 58.0;

struct HitState {
    points: Vec<(f64, f64)>,
    force_interactive: bool,
}

type SharedHitState = Arc<Mutex<HitState>>;

#[tauri::command]
fn update_hit_points(state: tauri::State<SharedHitState>, points: Vec<(f64, f64)>) {
    let mut s = state.lock().unwrap();
    s.points = points;
}

#[tauri::command]
fn set_force_interactive(state: tauri::State<SharedHitState>, active: bool) {
    let mut s = state.lock().unwrap();
    s.force_interactive = active;
}

#[tauri::command]
fn get_stage_size(window: WebviewWindow) -> (f64, f64) {
    if let Ok(Some(monitor)) = window.current_monitor() {
        let scale = monitor.scale_factor();
        let size = monitor.size();
        return (size.width as f64 / scale, size.height as f64 / scale);
    }
    (1280.0, 800.0)
}

#[cfg(target_os = "windows")]
fn get_cursor_pos_physical() -> Option<(f64, f64)> {
    unsafe {
        let mut point = POINT::default();
        if GetCursorPos(&mut point).is_ok() {
            Some((point.x as f64, point.y as f64))
        } else {
            None
        }
    }
}

#[cfg(target_os = "macos")]
fn get_cursor_pos_physical() -> Option<(f64, f64)> {
    let source = CGEventSource::new(CGEventSourceStateID::CombinedSessionState).ok()?;
    let event = CGEvent::new(source).ok()?;
    let point = event.location();
    Some((point.x, point.y))
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn get_cursor_pos_physical() -> Option<(f64, f64)> {
    None
}

fn cover_primary_monitor(window: &WebviewWindow) {
    if let Ok(Some(monitor)) = window.current_monitor() {
        let _ = window.set_position(tauri::Position::Physical(*monitor.position()));
        let _ = window.set_size(tauri::Size::Physical(*monitor.size()));
    }
}

fn toggle_charm(window: &WebviewWindow) {
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        cover_primary_monitor(window);
        let _ = window.show();
    }
}

fn start_hit_test_loop(app: tauri::AppHandle, state: SharedHitState) {
    thread::spawn(move || {
        let mut currently_interactive = false;
        loop {
            thread::sleep(Duration::from_millis(16));
            let Some(window) = app.get_webview_window("main") else {
                continue;
            };
            if !window.is_visible().unwrap_or(false) {
                continue;
            }

            let scale = window.scale_factor().unwrap_or(1.0);
            let window_pos = window
                .outer_position()
                .map(|p| (p.x as f64, p.y as f64))
                .unwrap_or((0.0, 0.0));

            let (force, points) = {
                let s = state.lock().unwrap();
                (s.force_interactive, s.points.clone())
            };

            let near_charm = if let Some((cx, cy)) = get_cursor_pos_physical() {
                let local_x = (cx - window_pos.0) / scale;
                let local_y = (cy - window_pos.1) / scale;
                let radius = if currently_interactive { HIT_RADIUS_EXIT } else { HIT_RADIUS_ENTER };
                points.iter().any(|(px, py)| {
                    let dx = local_x - px;
                    let dy = local_y - py;
                    (dx * dx + dy * dy).sqrt() < radius
                })
            } else {
                false
            };

            let should_be_interactive = force || near_charm;
            if should_be_interactive != currently_interactive {
                let _ = window.set_ignore_cursor_events(!should_be_interactive);
                currently_interactive = should_be_interactive;
            }
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let hit_state: SharedHitState = Arc::new(Mutex::new(HitState {
        points: Vec::new(),
        force_interactive: false,
    }));

    tauri::Builder::default()
        .manage(hit_state.clone())
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        if let Some(window) = app.get_webview_window("main") {
                            toggle_charm(&window);
                        }
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            update_hit_points,
            set_force_interactive,
            get_stage_size
        ])
        .setup(move |app| {
            let window = app.get_webview_window("main").expect("main window must exist");
            cover_primary_monitor(&window);
            let _ = window.set_ignore_cursor_events(true);
            let _ = window.show();

            start_hit_test_loop(app.handle().clone(), hit_state.clone());

            let shortcut = Shortcut::new(Some(Modifiers::SHIFT | Modifiers::ALT), Code::KeyK);
            app.global_shortcut().register(shortcut)?;

            let show_hide = MenuItem::with_id(app, "toggle", "Show/Hide Charm", true, None::<&str>)?;
            let recenter = MenuItem::with_id(app, "recenter", "Move to Top Center", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_hide, &recenter, &separator, &quit])?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .tooltip("DeskCharm — Shift+Alt+K to show/hide")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "toggle" => {
                        if let Some(window) = app.get_webview_window("main") {
                            toggle_charm(&window);
                        }
                    }
                    "recenter" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.emit("recenter", ());
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
