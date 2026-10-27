/*
 * clipem-obs: the OBS plugin that keeps ClipEm's OBS out of sight and tells
 * the tray widget (App/Shared/tray-widget/obs_notices.py) about everything
 * OBS would otherwise have shown the user.
 *
 * Loaded only by ClipEm's OBS: run.py points OBS_PLUGINS_PATH at
 * App/Shared/OBS-Plugin/<platform>/ (built by Dev/build/build-obs-plugin.sh).
 *
 * - Hidden OBS: removes OBS's own tray icon (ClipEm's is the only one), and
 *   when OBS was started with --minimize-to-tray but shows its window anyway
 *   (no tray host, e.g. GNOME without the AppIndicator extension), hides it
 *   again. The tray shows/hides the window over obs-websocket instead of
 *   clicking OBS's tray icon ("SetWindowVisible").
 * - Notices, each kept (the last MAX_NOTICES) and sent as a "Notice" vendor
 *   event, so the tray can catch up on any it missed with "Hello":
 *     dialog   - any dialog OBS opens while its window is hidden. One that
 *                only says OK is closed unseen once the tray has said Hello
 *                lately, as the tray shows it instead; one that asks
 *                something is left open for the user to answer.
 *     output   - the replay buffer, recording or stream stopping with an
 *                error (with OBS hidden, OBS only puts these on the tray
 *                icon this plugin removed).
 *     overload - frames being skipped ("Encoding overloaded").
 *     log      - every LOG_ERROR line, consecutive ones (a Python
 *                traceback) merged into one notice.
 *
 * No Q_OBJECT anywhere, so it builds without moc.
 */

#include <obs-frontend-api.h>
#include <obs-module.h>
#include <obs-websocket-api.h>
#include <util/base.h>
#include <util/platform.h>

#include <QAbstractButton>
#include <QApplication>
#include <QDialog>
#include <QEvent>
#include <QMainWindow>
#include <QMessageBox>
#include <QPointer>
#include <QSystemTrayIcon>
#include <QTextDocument>
#include <QTextDocumentFragment>
#include <QTimer>

#include <atomic>
#include <cctype>
#include <cstdarg>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <vector>

OBS_DECLARE_MODULE()

#define PLUGIN_VERSION 1
#define VENDOR_NAME "ClipEm"

static const size_t MAX_NOTICES = 200;
static const size_t MAX_TEXT = 4000;
// Consecutive LOG_ERROR lines this close together are one notice.
static const uint64_t LOG_MERGE_MS = 1500;
// The same notice again this soon only bumps its "repeats".
static const uint64_t REPEAT_WINDOW_MS = 60 * 1000;
// The tray says Hello every 10 s; this long without one, nobody is listening.
static const uint64_t LISTENER_TIMEOUT_MS = 30 * 1000;
static const int TICK_MS = 250;
static const uint64_t OVERLOAD_CHECK_MS = 2000;
static const uint64_t OVERLOAD_COOLDOWN_MS = 10 * 60 * 1000;
static const uint32_t OVERLOAD_SKIPPED_FRAMES = 10;

struct Notice {
	uint64_t id;
	uint64_t time_ms;
	std::string level; // error, warning, info
	std::string kind;  // dialog, output, overload, log
	std::string title;
	std::string text;
	bool needs_answer;
	bool dismissed;
	int repeats;
	bool sent;
};

static std::mutex notices_lock;
static std::deque<Notice> notices;
static uint64_t next_id = 1;
static std::string session;

static std::atomic<uint64_t> listener_seen_ms{0};
static std::atomic<bool> window_visible{false};
static bool visibility_sent = false;
static std::atomic<bool> shutting_down{false};

static obs_websocket_vendor vendor = nullptr;
static log_handler_t previous_log_handler = nullptr;
static void *previous_log_param = nullptr;
static thread_local bool in_log_handler = false;

static QPointer<QMainWindow> main_window;
static QPointer<QTimer> tick_timer;
static QObject *event_watch = nullptr;
static bool finished_loading = false;
static bool start_hidden = false;

static uint64_t now_ms()
{
	return os_gettime_ns() / 1000000;
}

static std::string clipped(std::string text)
{
	if (text.size() > MAX_TEXT) {
		text.resize(MAX_TEXT);
		text += "...";
	}
	return text;
}

static void add_notice(const char *level, const char *kind, const std::string &title, const std::string &text,
		       bool needs_answer = false, bool dismissed = false)
{
	std::lock_guard<std::mutex> guard(notices_lock);
	uint64_t now = now_ms();
	for (auto it = notices.rbegin(); it != notices.rend() && now - it->time_ms < REPEAT_WINDOW_MS; ++it) {
		if (it->kind == kind && it->title == title && it->text == text) {
			it->repeats++;
			return;
		}
	}
	notices.push_back({next_id++, now, level, kind, title, clipped(text), needs_answer, dismissed, 1, false});
	while (notices.size() > MAX_NOTICES)
		notices.pop_front();
}

static void add_log_error(const char *line)
{
	std::lock_guard<std::mutex> guard(notices_lock);
	uint64_t now = now_ms();
	if (!notices.empty()) {
		Notice &last = notices.back();
		if (last.kind == "log" && !last.sent && now - last.time_ms < LOG_MERGE_MS) {
			if (last.text.size() < MAX_TEXT)
				last.text = clipped(last.text + "\n" + line);
			last.time_ms = now;
			return;
		}
	}
	notices.push_back({next_id++, now, "error", "log", "OBS error", clipped(line), false, false, 1, false});
	while (notices.size() > MAX_NOTICES)
		notices.pop_front();
}

static obs_data_t *notice_data(const Notice &n)
{
	obs_data_t *d = obs_data_create();
	obs_data_set_int(d, "id", (long long)n.id);
	obs_data_set_string(d, "session", session.c_str());
	obs_data_set_string(d, "level", n.level.c_str());
	obs_data_set_string(d, "kind", n.kind.c_str());
	obs_data_set_string(d, "title", n.title.c_str());
	obs_data_set_string(d, "text", n.text.c_str());
	obs_data_set_bool(d, "needs_answer", n.needs_answer);
	obs_data_set_bool(d, "dismissed", n.dismissed);
	obs_data_set_int(d, "repeats", n.repeats);
	return d;
}

static bool listener_alive()
{
	uint64_t seen = listener_seen_ms.load();
	return seen && now_ms() - seen < LISTENER_TIMEOUT_MS;
}

/* ------------------------------------------------------------------------ */
/* LOG_ERROR lines                                                          */

static void log_handler(int level, const char *format, va_list args, void *param)
{
	if (level == LOG_ERROR && !in_log_handler) {
		in_log_handler = true;
		char line[2048];
		va_list copy;
		va_copy(copy, args);
		vsnprintf(line, sizeof(line), format, copy);
		va_end(copy);
		add_log_error(line);
		in_log_handler = false;
	}
	if (previous_log_handler)
		previous_log_handler(level, format, args, previous_log_param);
	(void)param;
}

/* ------------------------------------------------------------------------ */
/* Outputs stopping with an error                                           */

enum { OUT_REPLAY, OUT_RECORDING, OUT_STREAM, OUT_COUNT };
static const char *output_names[OUT_COUNT] = {"replay buffer", "recording", "stream"};
static obs_weak_output_t *hooked[OUT_COUNT] = {};

static void on_output_stop(void *data, calldata_t *cd)
{
	long long code = calldata_int(cd, "code");
	if (code == OBS_OUTPUT_SUCCESS)
		return;
	const char *what = output_names[(intptr_t)data];
	obs_output_t *output = (obs_output_t *)calldata_ptr(cd, "output");
	const char *last_error = output ? obs_output_get_last_error(output) : nullptr;

	std::string text = std::string("The ") + what + " stopped";
	switch (code) {
	case OBS_OUTPUT_NO_SPACE:
		text += ": the disk is full.";
		break;
	case OBS_OUTPUT_UNSUPPORTED:
		text += ": its encoder or format isn't supported. Check the encoder in Setup, or update your GPU driver.";
		break;
	case OBS_OUTPUT_ENCODE_ERROR:
		text += ": the encoder failed.";
		break;
	default:
		text += " because of an error.";
		break;
	}
	if (last_error && *last_error)
		text += std::string("\n") + last_error;
	std::string title = std::string(what) + " stopped";
	title[0] = (char)toupper((unsigned char)title[0]);
	add_notice("error", "output", title, text);
}

static void unhook_output(int i)
{
	if (!hooked[i])
		return;
	obs_output_t *output = obs_weak_output_get_output(hooked[i]);
	if (output) {
		signal_handler_disconnect(obs_output_get_signal_handler(output), "stop", on_output_stop,
					  (void *)(intptr_t)i);
		obs_output_release(output);
	}
	obs_weak_output_release(hooked[i]);
	hooked[i] = nullptr;
}

// OBS makes new output objects when its settings change, so this is redone
// each time one starts.
static void hook_output(int i, obs_output_t *output)
{
	if (!output)
		return;
	if (hooked[i] && obs_weak_output_references_output(hooked[i], output)) {
		obs_output_release(output);
		return;
	}
	unhook_output(i);
	signal_handler_connect(obs_output_get_signal_handler(output), "stop", on_output_stop, (void *)(intptr_t)i);
	hooked[i] = obs_output_get_weak_output(output);
	obs_output_release(output);
}

static void hook_outputs()
{
	hook_output(OUT_REPLAY, obs_frontend_get_replay_buffer_output());
	hook_output(OUT_RECORDING, obs_frontend_get_recording_output());
	hook_output(OUT_STREAM, obs_frontend_get_streaming_output());
}

/* ------------------------------------------------------------------------ */
/* Frames being skipped - OBS's "Encoding overloaded"                       */

static uint64_t overload_checked_ms = 0;
static uint64_t overload_notified_ms = 0;
static uint32_t last_skipped = 0;
static bool outputs_were_active = false;

static void check_overload()
{
	uint64_t now = now_ms();
	if (now - overload_checked_ms < OVERLOAD_CHECK_MS)
		return;
	overload_checked_ms = now;

	video_t *video = obs_get_video();
	if (!video)
		return;
	uint32_t skipped = video_output_get_skipped_frames(video);
	bool active = obs_frontend_replay_buffer_active() || obs_frontend_recording_active() ||
		      obs_frontend_streaming_active();
	bool count = active && outputs_were_active;
	outputs_were_active = active;
	uint32_t diff = skipped - last_skipped;
	last_skipped = skipped;
	if (!count || diff <= OVERLOAD_SKIPPED_FRAMES)
		return;
	if (overload_notified_ms && now - overload_notified_ms < OVERLOAD_COOLDOWN_MS)
		return;
	overload_notified_ms = now;
	add_notice("warning", "overload", "Encoding overloaded",
		   "OBS is skipping frames, so clips may stutter. A game using all of the GPU, or a slow encoder "
		   "preset, can cause this - try a lower resolution or frame rate in Setup.");
}

/* ------------------------------------------------------------------------ */
/* The main window and dialogs                                              */

static QMainWindow *get_main_window()
{
	if (!main_window)
		main_window = static_cast<QMainWindow *>(obs_frontend_get_main_window());
	return main_window;
}

static bool main_window_showing()
{
	QMainWindow *main = get_main_window();
	return main && main->isVisible() && !main->isMinimized();
}

static std::string plain(const QString &text)
{
	QString out = Qt::mightBeRichText(text) ? QTextDocumentFragment::fromHtml(text).toPlainText() : text;
	return out.trimmed().toStdString();
}

static void hide_tray_icon()
{
	QMainWindow *main = get_main_window();
	if (!main)
		return;
	for (QSystemTrayIcon *icon : main->findChildren<QSystemTrayIcon *>()) {
		if (icon->isVisible())
			icon->hide();
	}
}

// OBS's own show/hide (OBSBasic::SetShowing, a private slot): it also turns
// the preview off while hidden and brings back dialogs it hid.
static void set_window_visible(bool visible)
{
	QMainWindow *main = get_main_window();
	if (!main)
		return;
	if (visible && main->isVisible()) {
		main->setWindowState((main->windowState() & ~Qt::WindowMinimized) | Qt::WindowActive);
		main->raise();
		main->activateWindow();
		return;
	}
	if (!QMetaObject::invokeMethod(main, "SetShowing", Qt::DirectConnection, Q_ARG(bool, visible))) {
		blog(LOG_WARNING, "[clipem-obs] OBSBasic::SetShowing not found - showing/hiding the window directly");
		main->setVisible(visible);
		if (visible) {
			main->raise();
			main->activateWindow();
		}
	}
}

static void on_dialog_shown(QDialog *dialog)
{
	if (main_window_showing())
		return; // the user is looking at OBS - it's theirs to answer
	auto *box = qobject_cast<QMessageBox *>(dialog);
	std::string title = plain(dialog->windowTitle());
	if (!box) {
		add_notice("info", "dialog", title, "OBS opened \"" + title + "\".", true);
		return;
	}

	const char *level = box->icon() == QMessageBox::Critical  ? "error"
			    : box->icon() == QMessageBox::Warning ? "warning"
								  : "info";
	std::string text = plain(box->text());
	std::string more = plain(box->informativeText());
	if (!more.empty())
		text += (text.empty() ? "" : "\n") + more;

	QList<QAbstractButton *> buttons = box->buttons();
	QAbstractButton *ok = box->escapeButton();
	if (!ok && buttons.size() == 1)
		ok = buttons.first();
	if (buttons.size() > 1 || !ok || !listener_alive()) {
		add_notice(level, "dialog", title, text, buttons.size() > 1);
		return;
	}
	add_notice(level, "dialog", title, text, false, true);
	// The tray shows it instead: close it before it's ever drawn. Show events
	// come before the window is put on screen, so this keeps it off.
	box->setAttribute(Qt::WA_DontShowOnScreen, true);
	QPointer<QAbstractButton> button = ok;
	QTimer::singleShot(0, box, [button]() {
		if (button)
			button->click();
	});
}

class EventWatch : public QObject {
public:
	bool eventFilter(QObject *obj, QEvent *event) override
	{
		if (event->type() != QEvent::Show || shutting_down || !obj->isWidgetType())
			return false;
		auto *widget = static_cast<QWidget *>(obj);
		if (!widget->isWindow())
			return false;
		if (widget == get_main_window()) {
			// Shown at startup despite --minimize-to-tray: OBS does that
			// when there's no tray to hide to.
			if (start_hidden && !finished_loading) {
				blog(LOG_INFO, "[clipem-obs] OBS showed its window at startup - hiding it");
				QTimer::singleShot(0, widget, []() { set_window_visible(false); });
			}
			return false;
		}
		if (auto *dialog = qobject_cast<QDialog *>(widget))
			on_dialog_shown(dialog);
		return false;
	}
};

/* ------------------------------------------------------------------------ */
/* Sending notices and window changes to the tray                           */

static void emit_pending()
{
	if (!vendor)
		return;
	std::vector<obs_data_t *> pending;
	{
		std::lock_guard<std::mutex> guard(notices_lock);
		uint64_t now = now_ms();
		for (Notice &n : notices) {
			if (n.sent)
				continue;
			if (n.kind == "log" && now - n.time_ms < LOG_MERGE_MS)
				break; // more of it may still come; keeps the order too
			n.sent = true;
			pending.push_back(notice_data(n));
		}
	}
	for (obs_data_t *d : pending) {
		obs_websocket_vendor_emit_event(vendor, "Notice", d);
		obs_data_release(d);
	}
}

static void tick()
{
	if (shutting_down)
		return;
	hide_tray_icon();
	bool visible = main_window_showing();
	if (visible != window_visible.load() || !visibility_sent) {
		window_visible = visible;
		if (vendor) {
			obs_data_t *d = obs_data_create();
			obs_data_set_bool(d, "visible", visible);
			obs_websocket_vendor_emit_event(vendor, "WindowVisibility", d);
			obs_data_release(d);
			visibility_sent = true;
		}
	}
	check_overload();
	emit_pending();
}

/* ------------------------------------------------------------------------ */
/* Vendor requests (obs-websocket's thread)                                 */

// {after_id, session} -> {plugin_version, session, window_visible, notices}:
// every notice after after_id, or all of them if OBS restarted since
// (session changed). Also how the tray says it's listening.
static void request_hello(obs_data_t *request, obs_data_t *response, void *)
{
	listener_seen_ms = now_ms();
	long long after = obs_data_get_int(request, "after_id");
	bool same_session = session == obs_data_get_string(request, "session");
	obs_data_array_t *list = obs_data_array_create();
	{
		std::lock_guard<std::mutex> guard(notices_lock);
		for (const Notice &n : notices) {
			if (same_session && (long long)n.id <= after)
				continue;
			if (n.kind == "log" && !n.sent)
				continue; // still being merged; comes as an event
			obs_data_t *d = notice_data(n);
			obs_data_array_push_back(list, d);
			obs_data_release(d);
		}
	}
	obs_data_set_int(response, "plugin_version", PLUGIN_VERSION);
	obs_data_set_string(response, "session", session.c_str());
	obs_data_set_bool(response, "window_visible", window_visible.load());
	obs_data_set_array(response, "notices", list);
	obs_data_array_release(list);
}

static void request_set_window_visible(obs_data_t *request, obs_data_t *response, void *)
{
	listener_seen_ms = now_ms();
	bool visible = obs_data_get_bool(request, "visible");
	QMainWindow *main = get_main_window();
	bool ok = main && QMetaObject::invokeMethod(
				  main, [visible]() { set_window_visible(visible); }, Qt::QueuedConnection);
	obs_data_set_bool(response, "ok", ok);
}

static void request_get_window_state(obs_data_t *, obs_data_t *response, void *)
{
	obs_data_set_bool(response, "visible", window_visible.load());
}

/* ------------------------------------------------------------------------ */

static void on_frontend_event(enum obs_frontend_event event, void *)
{
	switch (event) {
	case OBS_FRONTEND_EVENT_FINISHED_LOADING:
		finished_loading = true;
		hook_outputs();
		break;
	case OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTING:
	case OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTED:
		hook_output(OUT_REPLAY, obs_frontend_get_replay_buffer_output());
		break;
	case OBS_FRONTEND_EVENT_RECORDING_STARTING:
	case OBS_FRONTEND_EVENT_RECORDING_STARTED:
		hook_output(OUT_RECORDING, obs_frontend_get_recording_output());
		break;
	case OBS_FRONTEND_EVENT_STREAMING_STARTING:
	case OBS_FRONTEND_EVENT_STREAMING_STARTED:
		hook_output(OUT_STREAM, obs_frontend_get_streaming_output());
		break;
	case OBS_FRONTEND_EVENT_EXIT:
		shutting_down = true;
		if (tick_timer)
			tick_timer->stop();
		if (event_watch) {
			qApp->removeEventFilter(event_watch);
			delete event_watch;
			event_watch = nullptr;
		}
		break;
	default:
		break;
	}
}

bool obs_module_load(void)
{
	session = std::to_string(os_gettime_ns());
	base_get_log_handler(&previous_log_handler, &previous_log_param);
	base_set_log_handler(log_handler, nullptr);

	start_hidden = qApp->arguments().contains("--minimize-to-tray");
	event_watch = new EventWatch();
	qApp->installEventFilter(event_watch);
	tick_timer = new QTimer(qApp);
	QObject::connect(tick_timer.data(), &QTimer::timeout, tick);
	tick_timer->start(TICK_MS);

	obs_frontend_add_event_callback(on_frontend_event, nullptr);
	blog(LOG_INFO, "[clipem-obs] loaded (version %d)", PLUGIN_VERSION);
	return true;
}

void obs_module_post_load(void)
{
	vendor = obs_websocket_register_vendor(VENDOR_NAME);
	if (!vendor) {
		blog(LOG_WARNING, "[clipem-obs] obs-websocket isn't loaded - ClipEm can't be told about notices");
		return;
	}
	obs_websocket_vendor_register_request(vendor, "Hello", request_hello, nullptr);
	obs_websocket_vendor_register_request(vendor, "SetWindowVisible", request_set_window_visible, nullptr);
	obs_websocket_vendor_register_request(vendor, "GetWindowState", request_get_window_state, nullptr);
}

void obs_module_unload(void)
{
	shutting_down = true;
	for (int i = 0; i < OUT_COUNT; i++)
		unhook_output(i);
	base_set_log_handler(previous_log_handler, previous_log_param);
}

const char *obs_module_name(void)
{
	return "ClipEm";
}

const char *obs_module_description(void)
{
	return "Keeps ClipEm's OBS hidden and passes its errors and messages on to the ClipEm tray.";
}
