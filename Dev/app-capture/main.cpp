// clipem-app-capture: records one Windows app's sound (its process and the
// processes it started) as raw 16-bit stereo 48 kHz PCM on stdout, for the tray
// widget's app tracks (App/Shared/tray-widget/app_recorder.py).
//
//   clipem-app-capture.exe --pid <pid>
//
// WASAPI process loopback - Windows 10 version 2004 (build 19041) or later; the
// same API as Microsoft's ApplicationLoopback sample. Runs until stdin closes
// (the tray widget closing it, or exiting) or stdout can't be written to.
//
// Built by Dev/build/build-app-capture.sh (mingw-w64 cross compiler).

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <windows.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <objidl.h>
#include <propidl.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

// audioclientactivationparams.h, which the mingw-w64 headers don't have yet.
namespace {
const wchar_t kProcessLoopbackDevice[] = L"VAD\\Process_Loopback";
enum ActivationType { ACTIVATION_DEFAULT = 0, ACTIVATION_PROCESS_LOOPBACK = 1 };
enum LoopbackMode { LOOPBACK_INCLUDE_TREE = 0, LOOPBACK_EXCLUDE_TREE = 1 };
struct ProcessLoopbackParams {
    DWORD TargetProcessId;
    int ProcessLoopbackMode;
};
struct ActivationParams {
    int ActivationType;
    ProcessLoopbackParams ProcessLoopbackParams;
};

const int kRate = 48000;
const int kChannels = 2;
const int kBytesPerFrame = 4;

std::atomic<bool> g_stop{false};

// Completes ActivateAudioInterfaceAsync. Must be agile (callable from any thread).
class ActivationHandler : public IActivateAudioInterfaceCompletionHandler, public IAgileObject {
public:
    ActivationHandler() : done_(CreateEventW(nullptr, TRUE, FALSE, nullptr)) {}
    ~ActivationHandler() { CloseHandle(done_); }

    HANDLE done() const { return done_; }

    STDMETHODIMP QueryInterface(REFIID riid, void **out) override {
        if (riid == IID_IUnknown || riid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *out = static_cast<IActivateAudioInterfaceCompletionHandler *>(this);
        } else if (riid == __uuidof(IAgileObject)) {
            *out = static_cast<IAgileObject *>(this);
        } else {
            *out = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return ++refs_; }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG left = --refs_;
        if (left == 0) delete this;
        return left;
    }
    STDMETHODIMP ActivateCompleted(IActivateAudioInterfaceAsyncOperation *) override {
        SetEvent(done_);
        return S_OK;
    }

private:
    std::atomic<ULONG> refs_{1};
    HANDLE done_;
};

DWORD WINAPI WatchStdin(LPVOID) {
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE);
    char buffer[64];
    DWORD got;
    while (ReadFile(in, buffer, sizeof buffer, &got, nullptr) && got > 0) {
    }
    g_stop = true;
    return 0;
}

bool WriteAll(HANDLE out, const BYTE *data, DWORD size) {
    while (size > 0) {
        DWORD written = 0;
        if (!WriteFile(out, data, size, &written, nullptr) || written == 0) return false;
        data += written;
        size -= written;
    }
    return true;
}

int Fail(const char *what, HRESULT hr) {
    fprintf(stderr, "clipem-app-capture: %s failed (0x%08lx)\n", what, static_cast<unsigned long>(hr));
    return 1;
}
}  // namespace

int wmain(int argc, wchar_t **argv) {
    DWORD pid = 0;
    for (int i = 1; i + 1 < argc; i++) {
        if (wcscmp(argv[i], L"--pid") == 0) pid = static_cast<DWORD>(wcstoul(argv[i + 1], nullptr, 10));
    }
    if (pid == 0) {
        fprintf(stderr, "usage: clipem-app-capture --pid <pid>\n");
        return 2;
    }

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) return Fail("CoInitializeEx", hr);

    ActivationParams params = {};
    params.ActivationType = ACTIVATION_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = LOOPBACK_INCLUDE_TREE;
    PROPVARIANT activate = {};
    activate.vt = VT_BLOB;
    activate.blob.cbSize = sizeof params;
    activate.blob.pBlobData = reinterpret_cast<BYTE *>(&params);

    ActivationHandler *handler = new ActivationHandler();
    IActivateAudioInterfaceAsyncOperation *operation = nullptr;
    hr = ActivateAudioInterfaceAsync(kProcessLoopbackDevice, __uuidof(IAudioClient), &activate, handler,
                                     &operation);
    if (FAILED(hr)) return Fail("ActivateAudioInterfaceAsync", hr);
    if (WaitForSingleObject(handler->done(), 5000) != WAIT_OBJECT_0) return Fail("activation (timed out)", E_FAIL);
    HRESULT activated = E_FAIL;
    IUnknown *unknown = nullptr;
    hr = operation->GetActivateResult(&activated, &unknown);
    operation->Release();
    handler->Release();
    if (FAILED(hr) || FAILED(activated) || !unknown) return Fail("activation", FAILED(hr) ? hr : activated);
    IAudioClient *client = nullptr;
    hr = unknown->QueryInterface(__uuidof(IAudioClient), reinterpret_cast<void **>(&client));
    unknown->Release();
    if (FAILED(hr)) return Fail("IAudioClient", hr);

    WAVEFORMATEX format = {};
    format.wFormatTag = WAVE_FORMAT_PCM;
    format.nChannels = kChannels;
    format.nSamplesPerSec = kRate;
    format.wBitsPerSample = 16;
    format.nBlockAlign = kBytesPerFrame;
    format.nAvgBytesPerSec = kRate * kBytesPerFrame;
    hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                            2000000 /* 200 ms */, 0, &format, nullptr);
    if (FAILED(hr)) return Fail("Initialize", hr);

    HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    hr = client->SetEventHandle(event);
    if (FAILED(hr)) return Fail("SetEventHandle", hr);
    IAudioCaptureClient *capture = nullptr;
    hr = client->GetService(__uuidof(IAudioCaptureClient), reinterpret_cast<void **>(&capture));
    if (FAILED(hr)) return Fail("GetService", hr);

    CreateThread(nullptr, 0, WatchStdin, nullptr, 0, nullptr);
    hr = client->Start();
    if (FAILED(hr)) return Fail("Start", hr);

    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    std::vector<BYTE> silence;
    while (!g_stop) {
        WaitForSingleObject(event, 50);
        UINT32 packet = 0;
        while (!g_stop && SUCCEEDED(capture->GetNextPacketSize(&packet)) && packet > 0) {
            BYTE *data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            if (FAILED(capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) break;
            DWORD size = frames * kBytesPerFrame;
            bool ok;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                silence.assign(size, 0);
                ok = WriteAll(out, silence.data(), size);
            } else {
                ok = WriteAll(out, data, size);
            }
            capture->ReleaseBuffer(frames);
            if (!ok) {
                g_stop = true;
                break;
            }
        }
    }
    client->Stop();
    capture->Release();
    client->Release();
    CoUninitialize();
    return 0;
}
