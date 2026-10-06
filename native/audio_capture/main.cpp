// lol_audio_capture.exe
//
// 특정 프로세스(League of Legends.exe)의 소리만 캡처해서 named pipe 로 PCM 을 흘려보내는 헬퍼.
// Process Loopback API 를 사용한다. Microsoft 문서상 최소 버전은 빌드 20348 이지만
// 실제로는 Windows 10 2004(빌드 19041) 이상에서 동작한다 (OBS 응용 프로그램 오디오 캡처와 동일).
// 지원되지 않는 환경에서는 "ERROR ..." 를 출력하고, 앱은 영상만 녹화한다.
//
// 사용법: lol_audio_capture.exe --pid <PID> --pipe \\.\pipe\name [--rate 48000] [--channels 2]
// 표준 출력: "READY" (pipe 준비 완료) 또는 "ERROR <설명>"
//
// 게임이 소리를 내지 않는 동안에는 API 가 데이터를 주지 않으므로, 경과 시간만큼 무음을 채워
// 영상과 소리의 싱크가 어긋나지 않게 한다.

#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <wrl/implements.h>
#include <wrl/client.h>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "mmdevapi.lib")

using Microsoft::WRL::ComPtr;

class ActivationHandler
    : public Microsoft::WRL::RuntimeClass<Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
                                          Microsoft::WRL::FtmBase, IActivateAudioInterfaceCompletionHandler> {
public:
    HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HRESULT result = E_FAIL;
    ComPtr<IAudioClient> client;

    ~ActivationHandler() { CloseHandle(done); }

    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* op) override {
        HRESULT activateHr = E_FAIL;
        ComPtr<IUnknown> unk;
        HRESULT hr = op->GetActivateResult(&activateHr, &unk);
        if (SUCCEEDED(hr) && SUCCEEDED(activateHr)) {
            result = unk.As(&client);
        } else {
            result = FAILED(hr) ? hr : activateHr;
        }
        SetEvent(done);
        return S_OK;
    }
};

static void fail(const char* what, HRESULT hr) {
    std::printf("ERROR %s (hr=0x%08lx)\n", what, static_cast<unsigned long>(hr));
    std::fflush(stdout);
    std::exit(1);
}

int wmain(int argc, wchar_t** argv) {
    DWORD pid = 0;
    std::wstring pipeName;
    UINT32 rate = 48000;
    UINT16 channels = 2;
    for (int i = 1; i + 1 < argc; i += 2) {
        std::wstring key = argv[i];
        if (key == L"--pid") pid = static_cast<DWORD>(_wtoi(argv[i + 1]));
        else if (key == L"--pipe") pipeName = argv[i + 1];
        else if (key == L"--rate") rate = static_cast<UINT32>(_wtoi(argv[i + 1]));
        else if (key == L"--channels") channels = static_cast<UINT16>(_wtoi(argv[i + 1]));
    }
    if (pid == 0 || pipeName.empty()) {
        std::printf("ERROR usage: --pid <pid> --pipe <\\\\.\\pipe\\name>\n");
        return 2;
    }

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) fail("CoInitializeEx", hr);

    // ---- 프로세스 루프백 오디오 클라이언트 활성화
    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    params.ProcessLoopbackParams.TargetProcessId = pid;

    PROPVARIANT pv = {};
    pv.vt = VT_BLOB;
    pv.blob.cbSize = sizeof(params);
    pv.blob.pBlobData = reinterpret_cast<BYTE*>(&params);

    auto handler = Microsoft::WRL::Make<ActivationHandler>();
    ComPtr<IActivateAudioInterfaceAsyncOperation> op;
    hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &pv,
                                     handler.Get(), &op);
    if (FAILED(hr)) fail("ActivateAudioInterfaceAsync (Windows 10 2004 이상 필요)", hr);
    WaitForSingleObject(handler->done, 10000);
    if (FAILED(handler->result) || !handler->client) fail("activation", handler->result);
    ComPtr<IAudioClient> client = handler->client;

    WAVEFORMATEX fmt = {};
    fmt.wFormatTag = WAVE_FORMAT_PCM;
    fmt.nChannels = channels;
    fmt.nSamplesPerSec = rate;
    fmt.wBitsPerSample = 16;
    fmt.nBlockAlign = static_cast<WORD>(channels * 2);
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign;

    hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
                            200000 /* 20ms */, 0, &fmt, nullptr);
    if (FAILED(hr)) fail("IAudioClient::Initialize", hr);

    HANDLE sampleReady = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    hr = client->SetEventHandle(sampleReady);
    if (FAILED(hr)) fail("SetEventHandle", hr);

    ComPtr<IAudioCaptureClient> capture;
    hr = client->GetService(IID_PPV_ARGS(&capture));
    if (FAILED(hr)) fail("GetService(IAudioCaptureClient)", hr);

    // ---- named pipe 준비
    HANDLE pipe = CreateNamedPipeW(pipeName.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1,
                                   1 << 20, 0, 0, nullptr);
    if (pipe == INVALID_HANDLE_VALUE) fail("CreateNamedPipe", HRESULT_FROM_WIN32(GetLastError()));
    std::printf("READY\n");
    std::fflush(stdout);

    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
        fail("ConnectNamedPipe", HRESULT_FROM_WIN32(GetLastError()));
    }

    hr = client->Start();
    if (FAILED(hr)) fail("IAudioClient::Start", hr);

    LARGE_INTEGER freq, t0, now;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);
    const UINT32 frameBytes = fmt.nBlockAlign;
    unsigned long long framesWritten = 0;
    std::vector<BYTE> silence(static_cast<size_t>(rate / 10) * frameBytes, 0);  // 최대 100ms 씩

    auto writeAll = [&](const BYTE* data, DWORD bytes) -> bool {
        while (bytes > 0) {
            DWORD written = 0;
            if (!WriteFile(pipe, data, bytes, &written, nullptr)) return false;
            data += written;
            bytes -= written;
        }
        return true;
    };

    bool running = true;
    while (running) {
        WaitForSingleObject(sampleReady, 10);

        UINT32 packet = 0;
        while (SUCCEEDED(capture->GetNextPacketSize(&packet)) && packet > 0) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            if (FAILED(capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) break;
            DWORD bytes = frames * frameBytes;
            bool ok;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                std::vector<BYTE> zeros(bytes, 0);
                ok = writeAll(zeros.data(), bytes);
            } else {
                ok = writeAll(data, bytes);
            }
            capture->ReleaseBuffer(frames);
            if (!ok) { running = false; break; }
            framesWritten += frames;
        }
        if (!running) break;

        // 게임이 조용한 동안 빈 시간만큼 무음으로 채움 (50ms 이상 뒤처졌을 때)
        QueryPerformanceCounter(&now);
        double elapsed = static_cast<double>(now.QuadPart - t0.QuadPart) / static_cast<double>(freq.QuadPart);
        long long expected = static_cast<long long>(elapsed * rate);
        long long behind = expected - static_cast<long long>(framesWritten);
        if (behind > static_cast<long long>(rate / 20)) {
            while (behind > 0) {
                long long chunk = behind < static_cast<long long>(rate / 10) ? behind : rate / 10;
                if (!writeAll(silence.data(), static_cast<DWORD>(chunk * frameBytes))) {
                    running = false;
                    break;
                }
                framesWritten += static_cast<unsigned long long>(chunk);
                behind -= chunk;
            }
        }
    }

    client->Stop();
    CloseHandle(pipe);
    CloseHandle(sampleReady);
    CoUninitialize();
    return 0;
}
