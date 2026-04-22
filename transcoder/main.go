package main

import (
    "bufio"
    "bytes"
    "context"
    "encoding/json"
    "errors"
    "fmt"
    "io"
    "math"
    "os"
    "os/signal"
    "path/filepath"
    "sort"
    "strconv"
    "strings"
    "sync"
    "syscall"
    "time"

    "github.com/moby/moby/api/pkg/stdcopy"
    "github.com/moby/moby/api/types/container"
    "github.com/moby/moby/api/types/mount"
    "github.com/moby/moby/api/types/network"
    "github.com/moby/moby/client"
)

const (
    defaultJobs        = 2
    defaultOutputRoot  = "/output"
    defaultInputRoot   = "/input"
    defaultProgressDir = "/progress"
    defaultFFmpegImage = "lscr.io/linuxserver/ffmpeg:latest"
)

type AppConfig struct {
    InputDir      string
    OutputDir     string
    ProgressDir   string
    FFmpegImage   string
    Concurrency   int
    PullIfMissing bool

    HostInputDir    string
    HostOutputDir   string
    HostProgressDir string
}

type FFProbe struct {
    Streams []ProbeStream `json:"streams"`
    Format  ProbeFormat   `json:"format"`
}

type ProbeFormat struct {
    Filename string            `json:"filename"`
    Duration string            `json:"duration"`
    Tags     map[string]string `json:"tags"`
}

type ProbeSideData struct {
    SideDataType string `json:"side_data_type"`
    DVProfile    int    `json:"dv_profile"`
    DVLevel      int    `json:"dv_level"`
    RPUPresent   int    `json:"rpu_present_flag"`
    ELPresent    int    `json:"el_present_flag"`
    BLPresent    int    `json:"bl_present_flag"`
}

type ProbeStream struct {
    Index          int               `json:"index"`
    CodecType      string            `json:"codec_type"`
    CodecName      string            `json:"codec_name"`
    Profile        string            `json:"profile"`
    Width          int               `json:"width"`
    Height         int               `json:"height"`
    PixFmt         string            `json:"pix_fmt"`
    ColorSpace     string            `json:"color_space"`
    ColorTransfer  string            `json:"color_transfer"`
    ColorPrimaries string            `json:"color_primaries"`
    Tags           map[string]string `json:"tags"`
    SideDataList   []ProbeSideData   `json:"side_data_list"`
}

type Job struct {
    InputPath   string
    BaseName    string
    DurationSec float64
    Width       int
    Height      int
    IsHDR       bool
    IsDV        bool
    Needs1080   bool
    NativeOut   string
    Out1080     string
}

type ProgressEvent struct {
    Key     string
    OutPath string
    Seconds float64
    Speed   string
    Done    bool
    Err     error

    MediaDurationSec float64
    WorkTotal        float64
}

type RunningState struct {
    mu sync.Mutex

    activeSec       map[string]float64
    activeSpd       map[string]string
    activeWorkTotal map[string]float64
    activeDurSec    map[string]float64

    completed float64
    total     float64

    doneCount int
    wantCount int
    started   time.Time
}

type ActiveContainers struct {
    mu  sync.Mutex
    ids map[string]struct{}
}

func (a *ActiveContainers) Add(id string) {
    a.mu.Lock()
    defer a.mu.Unlock()
    if a.ids == nil {
        a.ids = make(map[string]struct{})
    }
    a.ids[id] = struct{}{}
}

func (a *ActiveContainers) Remove(id string) {
    a.mu.Lock()
    defer a.mu.Unlock()
    delete(a.ids, id)
}

func (a *ActiveContainers) Snapshot() []string {
    a.mu.Lock()
    defer a.mu.Unlock()
    out := make([]string, 0, len(a.ids))
    for id := range a.ids {
        out = append(out, id)
    }
    return out
}

func main() {
    cfg := AppConfig{
        InputDir:      getenv("INPUT_DIR", defaultInputRoot),
        OutputDir:     getenv("OUTPUT_DIR", defaultOutputRoot),
        ProgressDir:   getenv("PROGRESS_DIR", defaultProgressDir),
        FFmpegImage:   getenv("FFMPEG_IMAGE", defaultFFmpegImage),
        Concurrency:   getenvInt("JOBS", defaultJobs),
        PullIfMissing: getenvBool("PULL_MISSING", true),
    }
    if cfg.Concurrency < 1 {
        cfg.Concurrency = 1
    }

    if err := os.MkdirAll(cfg.OutputDir, 0o755); err != nil {
        failf("create output dir: %v", err)
    }
    if err := os.MkdirAll(cfg.ProgressDir, 0o755); err != nil {
        failf("create progress dir: %v", err)
    }

    cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
    if err != nil {
        failf("docker client init: %v", err)
    }

    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()

    hostIn, hostOut, hostProg, err := detectHostMountSources(ctx, cli, cfg.InputDir, cfg.OutputDir, cfg.ProgressDir)
    if err != nil {
        failf("detect mount sources: %v", err)
    }
    cfg.HostInputDir = hostIn
    cfg.HostOutputDir = hostOut
    cfg.HostProgressDir = hostProg

    fmt.Printf("batch-transcoder\n")
    fmt.Printf("Input (container):    %s -> source: %s\n", cfg.InputDir, cfg.HostInputDir)
    fmt.Printf("Output (container):   %s -> source: %s\n", cfg.OutputDir, cfg.HostOutputDir)
    fmt.Printf("Progress (container): %s -> source: %s\n", cfg.ProgressDir, cfg.HostProgressDir)
    fmt.Printf("FFmpeg image:         %s\n", cfg.FFmpegImage)
    fmt.Printf("Concurrency:          %d\n\n", cfg.Concurrency)

    if cfg.PullIfMissing {
        if err := ensureImage(ctx, cli, cfg.FFmpegImage); err != nil {
            failf("ensure ffmpeg image: %v", err)
        }
    }

    active := &ActiveContainers{ids: make(map[string]struct{})}

    go func() {
        <-ctx.Done()
        cleanupCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
        defer cancel()
        for _, id := range active.Snapshot() {
            _ = forceRemoveContainer(cleanupCtx, cli, id)
        }
    }()

    jobs, err := discoverJobs(ctx, cli, cfg)
    if err != nil {
        failf("discover jobs: %v", err)
    }
    if len(jobs) == 0 {
        fmt.Println("No work to do.")
        return
    }

    sort.Slice(jobs, func(i, j int) bool { return jobs[i].DurationSec > jobs[j].DurationSec })

    state := &RunningState{
        activeSec:       make(map[string]float64),
        activeSpd:       make(map[string]string),
        activeWorkTotal: make(map[string]float64),
        activeDurSec:    make(map[string]float64),
        started:         time.Now(),
    }

    for _, j := range jobs {
        state.total += workUnits(j.DurationSec, j.Width, j.Height)
        state.wantCount++
        if j.Needs1080 {
            state.total += workUnits(j.DurationSec, 1920, 1080)
            state.wantCount++
        }
    }

    fmt.Printf("Queued %d source file(s)\n", len(jobs))
    for _, j := range jobs {
        fmt.Printf("- %s [%dx%d dur=%s HDR=%v DV=%v 1080=%v]\n",
            filepath.Base(j.InputPath), j.Width, j.Height, fmtDurSeconds(j.DurationSec), j.IsHDR, j.IsDV, j.Needs1080)
        fmt.Printf("  native: %s\n", filepath.Base(j.NativeOut))
        if j.Needs1080 {
            fmt.Printf("  1080p : %s\n", filepath.Base(j.Out1080))
        }
    }
    fmt.Println()

    progressCh := make(chan ProgressEvent, 256)
    jobCh := make(chan Job)

    var wg sync.WaitGroup
    for i := 0; i < cfg.Concurrency; i++ {
        wg.Add(1)
        go func() {
            defer wg.Done()
            for job := range jobCh {
                if ctx.Err() != nil {
                    return
                }
                processJob(ctx, cli, cfg, active, job, progressCh)
            }
        }()
    }

    go func() {
        defer close(progressCh)
        for _, j := range jobs {
            select {
            case <-ctx.Done():
                close(jobCh)
                wg.Wait()
                return
            case jobCh <- j:
            }
        }
        close(jobCh)
        wg.Wait()
    }()

    ticker := time.NewTicker(1 * time.Second)
    defer ticker.Stop()

    for {
        select {
        case <-ctx.Done():
            printOverall(state, true)
            fmt.Printf("\nCancelled: %v\n", ctx.Err())
            return
        case ev, ok := <-progressCh:
            if !ok {
                printOverall(state, true)
                fmt.Println("\nAll done.")
                return
            }
            applyProgress(state, ev)
        case <-ticker.C:
            printOverall(state, false)
        }
    }
}

func discoverJobs(ctx context.Context, cli *client.Client, cfg AppConfig) ([]Job, error) {
    entries, err := os.ReadDir(cfg.InputDir)
    if err != nil {
        return nil, err
    }

    var jobs []Job
    for _, e := range entries {
        if e.IsDir() {
            continue
        }
        ext := strings.ToLower(filepath.Ext(e.Name()))
        switch ext {
        case ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts":
        default:
            continue
        }

        containerInput := filepath.Join(cfg.InputDir, e.Name())
        meta, err := probeFile(ctx, cli, cfg, containerInput)
        if err != nil {
            fmt.Fprintf(os.Stderr, "probe failed for %s: %v\n", containerInput, err)
            continue
        }

        v, dur, err := selectVideo(meta)
        if err != nil {
            fmt.Fprintf(os.Stderr, "skip %s: %v\n", containerInput, err)
            continue
        }

        isDV := hasDV(v)
        isHDRFlag := isHDR(v) || isDV
        needs1080 := isHDRFlag || v.Width >= 3840 || v.Height >= 2160

        base := strings.TrimSuffix(e.Name(), filepath.Ext(e.Name()))
        hdrSuffix := isDV

        native := filepath.Join(cfg.OutputDir, buildOutputName(base, false, hdrSuffix))
        var out1080 string
        if needs1080 {
            out1080 = filepath.Join(cfg.OutputDir, buildOutputName(base, true, hdrSuffix))
        }

        if exists(native) && (!needs1080 || exists(out1080)) {
            fmt.Fprintf(os.Stderr, "Skipping existing outputs for %s\n", e.Name())
            continue
        }

        jobs = append(jobs, Job{
            InputPath:   containerInput,
            BaseName:    base,
            DurationSec: dur,
            Width:       v.Width,
            Height:      v.Height,
            IsHDR:       isHDRFlag,
            IsDV:        isDV,
            Needs1080:   needs1080,
            NativeOut:   native,
            Out1080:     out1080,
        })
    }
    return jobs, nil
}

func processJob(ctx context.Context, cli *client.Client, cfg AppConfig, active *ActiveContainers, job Job, progressCh chan<- ProgressEvent) {
    {
        outW, outH := job.Width, job.Height
        workTotal := workUnits(job.DurationSec, outW, outH)

        if !exists(job.NativeOut) {
            if err := runFFmpeg(ctx, cli, cfg, active, job, false, progressCh); err != nil {
                progressCh <- ProgressEvent{
                    Key:              labelFor(job, false),
                    OutPath:          job.NativeOut,
                    Err:              err,
                    Done:             true,
                    MediaDurationSec: job.DurationSec,
                    WorkTotal:        workTotal,
                }
            }
        } else {
            progressCh <- ProgressEvent{
                Key:              labelFor(job, false),
                OutPath:          job.NativeOut,
                Seconds:          job.DurationSec,
                Done:             true,
                MediaDurationSec: job.DurationSec,
                WorkTotal:        workTotal,
            }
        }
    }

    if job.Needs1080 {
        workTotal := workUnits(job.DurationSec, 1920, 1080)

        if !exists(job.Out1080) {
            if err := runFFmpeg(ctx, cli, cfg, active, job, true, progressCh); err != nil {
                progressCh <- ProgressEvent{
                    Key:              labelFor(job, true),
                    OutPath:          job.Out1080,
                    Err:              err,
                    Done:             true,
                    MediaDurationSec: job.DurationSec,
                    WorkTotal:        workTotal,
                }
            }
        } else {
            progressCh <- ProgressEvent{
                Key:              labelFor(job, true),
                OutPath:          job.Out1080,
                Seconds:          job.DurationSec,
                Done:             true,
                MediaDurationSec: job.DurationSec,
                WorkTotal:        workTotal,
            }
        }
    }
}

func runFFmpeg(ctx context.Context, cli *client.Client, cfg AppConfig, active *ActiveContainers, job Job, make1080 bool, progressCh chan<- ProgressEvent) error {
    containerInput := job.InputPath
    containerOutput := job.NativeOut
    if make1080 {
        containerOutput = job.Out1080
    }

    inFile := filepath.Base(containerInput)
    outFile := filepath.Base(containerOutput)

    containerInputPath := filepath.Join("/work/input", inFile)
    containerOutputPath := filepath.Join("/work/output", outFile)

    progressName := safeProgressFileName(labelFor(job, make1080)) + ".progress"
    hostProgressPath := filepath.Join(cfg.ProgressDir, progressName)
    containerProgressPath := filepath.Join("/work/progress", progressName)

    _ = os.Remove(hostProgressPath)

    vf := buildFilter(job, make1080)
    x265 := buildX265Params()

    ffCmd := []string{
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-y",
        "-progress", containerProgressPath,
        "-stats_period", "1",
        "-i", containerInputPath,
        "-map", "0",
        "-map_metadata", "0",
        "-map_chapters", "0",
    }
    if vf != "" {
        ffCmd = append(ffCmd, "-vf", vf)
    }
    ffCmd = append(ffCmd,
        "-c:v", "libx265",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p10le",
        "-profile:v", "main10",
        "-x265-params", x265,
        "-c:a", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        containerOutputPath,
    )

    createRes, err := cli.ContainerCreate(ctx, client.ContainerCreateOptions{
        Config: &container.Config{
            Image: cfg.FFmpegImage,
            Cmd:   ffCmd,
            Tty:   false,
        },
        HostConfig: &container.HostConfig{
            AutoRemove: false,
            Mounts: []mount.Mount{
                {Type: mount.TypeBind, Source: cfg.HostInputDir, Target: "/work/input", ReadOnly: true},
                {Type: mount.TypeBind, Source: cfg.HostOutputDir, Target: "/work/output", ReadOnly: false},
                {Type: mount.TypeBind, Source: cfg.HostProgressDir, Target: "/work/progress", ReadOnly: false},
            },
            NetworkMode: "none",
            SecurityOpt: []string{"no-new-privileges:true"},
        },
        NetworkingConfig: &network.NetworkingConfig{},
    })
    if err != nil {
        return fmt.Errorf("container create: %w", err)
    }

    id := createRes.ID
    active.Add(id)
    defer active.Remove(id)

    defer func() {
        cctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
        defer cancel()
        _ = forceRemoveContainer(cctx, cli, id)
        _ = os.Remove(hostProgressPath)
    }()

    if _, err := cli.ContainerStart(ctx, id, client.ContainerStartOptions{}); err != nil {
        return fmt.Errorf("container start: %w", err)
    }

    outW, outH := job.Width, job.Height
    if make1080 {
        outW, outH = 1920, 1080
    }
    workTotal := workUnits(job.DurationSec, outW, outH)

    progressDone := make(chan struct{})
    go func() {
        defer close(progressDone)
        tailProgressFile(ctx, hostProgressPath, labelFor(job, make1080), containerOutput, job.DurationSec, workTotal, progressCh)
    }()

    waitRes := cli.ContainerWait(ctx, id, client.ContainerWaitOptions{
        Condition: container.WaitConditionNotRunning,
    })

    select {
    case err := <-waitRes.Error:
        <-progressDone
        if err != nil {
            if ctx.Err() != nil {
                return ctx.Err()
            }
            return fmt.Errorf("container wait error: %w", err)
        }
    case res := <-waitRes.Result:
        <-progressDone
        if res.Error != nil {
            if ctx.Err() != nil {
                return ctx.Err()
            }
            return fmt.Errorf("ffmpeg wait error: %s", res.Error.Message)
        }
        if res.StatusCode != 0 {
            if ctx.Err() != nil {
                return ctx.Err()
            }
            return fmt.Errorf("ffmpeg exited with status %d", res.StatusCode)
        }
    }

    progressCh <- ProgressEvent{
        Key:              labelFor(job, make1080),
        OutPath:          containerOutput,
        Seconds:          job.DurationSec,
        Done:             true,
        MediaDurationSec: job.DurationSec,
        WorkTotal:        workTotal,
    }
    return nil
}

func tailProgressFile(ctx context.Context, path, key, outPath string, duration float64, workTotal float64, ch chan<- ProgressEvent) {
    deadline := time.Now().Add(30 * time.Second)
    for {
        if ctx.Err() != nil {
            return
        }
        if exists(path) {
            break
        }
        if time.Now().After(deadline) {
            return
        }
        time.Sleep(200 * time.Millisecond)
    }

    f, err := os.Open(path)
    if err != nil {
        return
    }
    defer f.Close()

    reader := bufio.NewReaderSize(f, 64*1024)
    var speed string

    for {
        if ctx.Err() != nil {
            return
        }

        line, err := reader.ReadString('\n')
        if err != nil {
            if errors.Is(err, io.EOF) {
                time.Sleep(200 * time.Millisecond)
                continue
            }
            return
        }

        line = strings.TrimSpace(line)
        if line == "" {
            continue
        }

        eq := strings.IndexByte(line, '=')
        if eq <= 0 {
            continue
        }
        k := line[:eq]
        v := strings.TrimSpace(line[eq+1:])

        switch k {
        case "speed":
            speed = v
        case "out_time_ms":
            us, _ := strconv.ParseFloat(v, 64)
            ch <- ProgressEvent{
                Key:              key,
                OutPath:          outPath,
                Seconds:          us / 1_000_000.0,
                Speed:            speed,
                MediaDurationSec: duration,
                WorkTotal:        workTotal,
            }
        case "out_time_us":
            us, _ := strconv.ParseFloat(v, 64)
            ch <- ProgressEvent{
                Key:              key,
                OutPath:          outPath,
                Seconds:          us / 1_000_000.0,
                Speed:            speed,
                MediaDurationSec: duration,
                WorkTotal:        workTotal,
            }
        case "out_time":
            if sec, ok := parseFFmpegTime(v); ok {
                ch <- ProgressEvent{
                    Key:              key,
                    OutPath:          outPath,
                    Seconds:          sec,
                    Speed:            speed,
                    MediaDurationSec: duration,
                    WorkTotal:        workTotal,
                }
            }
        case "progress":
            if v == "end" {
                return
            }
        }
    }
}

func probeFile(ctx context.Context, cli *client.Client, cfg AppConfig, containerInput string) (*FFProbe, error) {
    inFile := filepath.Base(containerInput)
    containerInputPath := filepath.Join("/work/input", inFile)

    cmd := []string{
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-print_format", "json",
        containerInputPath,
    }

    createRes, err := cli.ContainerCreate(ctx, client.ContainerCreateOptions{
        Config: &container.Config{
            Image:        cfg.FFmpegImage,
            Entrypoint:   []string{"ffprobe"},
            Cmd:          cmd,
            Tty:          false,
            AttachStdout: true,
            AttachStderr: true,
        },
        HostConfig: &container.HostConfig{
            AutoRemove: false,
            Mounts: []mount.Mount{
                {Type: mount.TypeBind, Source: cfg.HostInputDir, Target: "/work/input", ReadOnly: true},
            },
            NetworkMode: "none",
            SecurityOpt: []string{"no-new-privileges:true"},
        },
        NetworkingConfig: &network.NetworkingConfig{},
    })
    if err != nil {
        return nil, err
    }
    id := createRes.ID

    defer func() {
        cctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()
        _ = forceRemoveContainer(cctx, cli, id)
    }()

    attachRes, err := cli.ContainerAttach(ctx, id, client.ContainerAttachOptions{
        Stream: true,
        Stdout: true,
        Stderr: true,
        Logs:   false,
    })
    if err != nil {
        return nil, err
    }
    defer attachRes.Close()

    if _, err := cli.ContainerStart(ctx, id, client.ContainerStartOptions{}); err != nil {
        return nil, err
    }

    var stdout bytes.Buffer
    var stderr bytes.Buffer
    _, err = stdcopy.StdCopy(&stdout, &stderr, attachRes.Reader)
    if err != nil {
        return nil, err
    }

    waitRes := cli.ContainerWait(ctx, id, client.ContainerWaitOptions{
        Condition: container.WaitConditionNotRunning,
    })
    select {
    case err := <-waitRes.Error:
        if err != nil {
            if ctx.Err() != nil {
                return nil, ctx.Err()
            }
            return nil, err
        }
    case res := <-waitRes.Result:
        if res.Error != nil {
            if ctx.Err() != nil {
                return nil, ctx.Err()
            }
            return nil, fmt.Errorf("ffprobe wait error: %s", res.Error.Message)
        }
        if res.StatusCode != 0 {
            if ctx.Err() != nil {
                return nil, ctx.Err()
            }
            return nil, fmt.Errorf("ffprobe status %d: %s", res.StatusCode, stderr.String())
        }
    }

    var out FFProbe
    if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
        return nil, fmt.Errorf("parse ffprobe json: %w", err)
    }
    return &out, nil
}

func ensureImage(ctx context.Context, cli *client.Client, ref string) error {
    _, err := cli.ImageInspect(ctx, ref)
    if err == nil {
        return nil
    }

    resp, err := cli.ImagePull(ctx, ref, client.ImagePullOptions{})
    if err != nil {
        return err
    }
    defer resp.Close()

    _, _ = io.Copy(io.Discard, resp)
    return nil
}

func forceRemoveContainer(ctx context.Context, cli *client.Client, id string) error {
    timeout := 0
    _, _ = cli.ContainerStop(ctx, id, client.ContainerStopOptions{
        Timeout: &timeout,
    })
    _, err := cli.ContainerRemove(ctx, id, client.ContainerRemoveOptions{
        Force:         true,
        RemoveVolumes: true,
    })
    return err
}

func selectVideo(meta *FFProbe) (ProbeStream, float64, error) {
    for _, s := range meta.Streams {
        if s.CodecType == "video" {
            d, _ := strconv.ParseFloat(meta.Format.Duration, 64)
            if d <= 0 {
                d = 1
            }
            return s, d, nil
        }
    }
    return ProbeStream{}, 0, errors.New("no video stream")
}

func hasDV(v ProbeStream) bool {
    for _, sd := range v.SideDataList {
        t := strings.ToLower(sd.SideDataType)
        if strings.Contains(t, "dovi") || strings.Contains(t, "dolby vision") {
            return true
        }
        if sd.DVProfile > 0 || sd.RPUPresent > 0 || sd.ELPresent > 0 || sd.BLPresent > 0 {
            return true
        }
    }
    return false
}

func isHDR(v ProbeStream) bool {
    cp := strings.ToLower(v.ColorPrimaries)
    ct := strings.ToLower(v.ColorTransfer)
    cs := strings.ToLower(v.ColorSpace)

    return cp == "bt2020" ||
        ct == "smpte2084" ||
        ct == "arib-std-b67" ||
        cs == "bt2020nc" ||
        cs == "bt2020c"
}

func buildOutputName(base string, is1080 bool, hdrSuffix bool) string {
    var b strings.Builder
    b.WriteString(base)
    b.WriteString(".h265.crf18")
    if is1080 {
        b.WriteString(".1080")
    }
    if hdrSuffix {
        b.WriteString(".hdr")
    }
    b.WriteString(".mkv")
    return b.String()
}

func buildFilter(job Job, make1080 bool) string {
    if job.IsDV {
        if make1080 {
            return "zscale=t=linear:npl=100,format=gbrpf32le,zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc,tonemap=mobius:desat=0,zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc:range=limited,format=yuv420p10le,scale=1920:1080:flags=lanczos"
        }
        return "zscale=t=linear:npl=100,format=gbrpf32le,zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc,tonemap=mobius:desat=0,zscale=primaries=bt2020:transfer=smpte2084:matrix=bt2020nc:range=limited,format=yuv420p10le"
    }

    if make1080 {
        return "zscale=1920:1080:filter=lanczos,format=yuv420p10le"
    }
    return "format=yuv420p10le"
}

func buildX265Params() string {
    return strings.Join([]string{
        "hdr-opt=1",
        "repeat-headers=1",
        "colorprim=bt2020",
        "transfer=smpte2084",
        "colormatrix=bt2020nc",
        "range=limited",
    }, ":")
}

func parseFFmpegTime(s string) (float64, bool) {
    parts := strings.SplitN(s, ".", 2)
    hms := strings.Split(parts[0], ":")
    if len(hms) != 3 {
        return 0, false
    }
    h, err1 := strconv.Atoi(hms[0])
    m, err2 := strconv.Atoi(hms[1])
    sec, err3 := strconv.Atoi(hms[2])
    if err1 != nil || err2 != nil || err3 != nil {
        return 0, false
    }
    out := float64(h*3600 + m*60 + sec)
    if len(parts) == 2 && parts[1] != "" {
        frac, err := strconv.ParseFloat("0."+parts[1], 64)
        if err == nil {
            out += frac
        }
    }
    return out, true
}

func applyProgress(state *RunningState, ev ProgressEvent) {
    state.mu.Lock()
    defer state.mu.Unlock()

    if ev.MediaDurationSec > 0 {
        state.activeDurSec[ev.Key] = ev.MediaDurationSec
    }
    if ev.WorkTotal > 0 {
        state.activeWorkTotal[ev.Key] = ev.WorkTotal
    }

    if ev.Done {
        delete(state.activeSec, ev.Key)
        delete(state.activeSpd, ev.Key)
        delete(state.activeWorkTotal, ev.Key)
        delete(state.activeDurSec, ev.Key)

        state.completed += ev.WorkTotal
        state.doneCount++

        if ev.Err != nil {
            fmt.Printf("\nERROR: %s: %v\n", ev.Key, ev.Err)
        } else {
            fmt.Printf("\nDONE: %s -> %s\n", ev.Key, filepath.Base(ev.OutPath))
        }
        return
    }

    state.activeSec[ev.Key] = ev.Seconds
    if ev.Speed != "" {
        state.activeSpd[ev.Key] = ev.Speed
    }
}

func printOverall(state *RunningState, final bool) {
    state.mu.Lock()
    defer state.mu.Unlock()

    activeWork := 0.0
    for k, sec := range state.activeSec {
        dur := state.activeDurSec[k]
        tot := state.activeWorkTotal[k]
        if dur <= 0 || tot <= 0 {
            continue
        }
        frac := sec / dur
        if frac < 0 {
            frac = 0
        }
        if frac > 1 {
            frac = 1
        }
        activeWork += tot * frac
    }

    doneWork := state.completed + activeWork

    pct := 0.0
    if state.total > 0 {
        pct = doneWork / state.total * 100
    }
    pct = math.Min(pct, 100.0)

    elapsed := time.Since(state.started)
    eta := time.Duration(0)
    if doneWork > 0 && !final {
        totalEstimate := elapsed.Seconds() * (state.total / doneWork)
        remain := totalEstimate - elapsed.Seconds()
        if remain > 0 {
            eta = time.Duration(remain * float64(time.Second))
        }
    }

    names := make([]string, 0, len(state.activeSec))
    for k := range state.activeSec {
        names = append(names, k)
    }
    sort.Strings(names)

    var b strings.Builder
    fmt.Fprintf(&b, "\rOverall: %6.2f%% | outputs %d/%d | elapsed %s | ETA %s",
        pct, state.doneCount, state.wantCount, fmtDur(elapsed), fmtDur(eta))

    if len(names) > 0 {
        b.WriteString(" | active: ")
        for i, n := range names {
            if i > 0 {
                b.WriteString("; ")
            }
            b.WriteString(n)
            b.WriteString("=")
            b.WriteString(fmtDur(time.Duration(state.activeSec[n] * float64(time.Second))))
            if spd := state.activeSpd[n]; spd != "" {
                b.WriteString("@")
                b.WriteString(spd)
            }
        }
    }
    fmt.Print(b.String())
}

func labelFor(job Job, is1080 bool) string {
    if is1080 {
        return filepath.Base(job.InputPath) + " [1080p]"
    }
    return filepath.Base(job.InputPath) + " [native]"
}

func safeProgressFileName(s string) string {
    s = strings.ReplaceAll(s, "/", "_")
    s = strings.ReplaceAll(s, "\\", "_")
    s = strings.ReplaceAll(s, " ", "_")
    s = strings.ReplaceAll(s, "[", "")
    s = strings.ReplaceAll(s, "]", "")
    s = strings.ReplaceAll(s, ":", "_")
    return s
}

func detectHostMountSources(ctx context.Context, cli *client.Client, inputTarget, outputTarget, progressTarget string) (string, string, string, error) {
    selfID, err := selfContainerID()
    if err != nil {
        return "", "", "", err
    }

    inspect, err := cli.ContainerInspect(ctx, selfID, client.ContainerInspectOptions{})
    if err != nil {
        return "", "", "", fmt.Errorf("container inspect self (%s): %w", selfID, err)
    }

    var hostIn, hostOut, hostProg string
    for _, m := range inspect.Container.Mounts {
        if m.Type != "bind" && m.Type != "volume" {
            continue
        }
        if samePath(m.Destination, inputTarget) {
            hostIn = m.Source
        }
        if samePath(m.Destination, outputTarget) {
            hostOut = m.Source
        }
        if samePath(m.Destination, progressTarget) {
            hostProg = m.Source
        }
    }

    if hostIn == "" {
        return "", "", "", fmt.Errorf("could not find mount source for %s", inputTarget)
    }
    if hostOut == "" {
        return "", "", "", fmt.Errorf("could not find mount source for %s", outputTarget)
    }
    if hostProg == "" {
        return "", "", "", fmt.Errorf("could not find mount source for %s", progressTarget)
    }

    return hostIn, hostOut, hostProg, nil
}

func selfContainerID() (string, error) {
    if v := strings.TrimSpace(os.Getenv("HOSTNAME")); v != "" {
        return v, nil
    }

    b, err := os.ReadFile("/proc/self/cgroup")
    if err != nil {
        return "", fmt.Errorf("read /proc/self/cgroup: %w", err)
    }
    for _, ln := range strings.Split(string(b), "\n") {
        if id := findHex64(ln); id != "" {
            return id, nil
        }
    }
    return "", errors.New("could not determine container ID")
}

func findHex64(s string) string {
    isHex := func(c byte) bool {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')
    }
    for i := 0; i+64 <= len(s); i++ {
        sub := s[i : i+64]
        ok := true
        for j := 0; j < 64; j++ {
            c := sub[j]
            if c >= 'A' && c <= 'F' {
                c = c - 'A' + 'a'
            }
            if !isHex(c) {
                ok = false
                break
            }
        }
        if ok {
            return sub
        }
    }
    return ""
}

func samePath(a, b string) bool {
    aa := strings.TrimRight(a, "/")
    bb := strings.TrimRight(b, "/")
    if aa == "" {
        aa = "/"
    }
    if bb == "" {
        bb = "/"
    }
    return aa == bb
}

func exists(path string) bool {
    _, err := os.Stat(path)
    return err == nil
}

func fmtDur(d time.Duration) string {
    if d < 0 {
        d = 0
    }
    d = d.Round(time.Second)
    h := int(d.Hours())
    m := int(d.Minutes()) % 60
    s := int(d.Seconds()) % 60
    return fmt.Sprintf("%02d:%02d:%02d", h, m, s)
}

func fmtDurSeconds(sec float64) string {
    if sec < 0 {
        sec = 0
    }
    return fmtDur(time.Duration(sec * float64(time.Second)))
}

func workUnits(durationSec float64, w, h int) float64 {
    if durationSec <= 0 {
        durationSec = 1
    }
    if w <= 0 || h <= 0 {
        w, h = 1, 1
    }
    return durationSec * float64(w) * float64(h)
}

func getenv(k, def string) string {
    v := strings.TrimSpace(os.Getenv(k))
    if v == "" {
        return def
    }
    return v
}

func getenvInt(k string, def int) int {
    v := strings.TrimSpace(os.Getenv(k))
    if v == "" {
        return def
    }
    n, err := strconv.Atoi(v)
    if err != nil {
        return def
    }
    return n
}

func getenvBool(k string, def bool) bool {
    v := strings.TrimSpace(os.Getenv(k))
    if v == "" {
        return def
    }
    switch strings.ToLower(v) {
    case "1", "true", "yes", "y", "on":
        return true
    case "0", "false", "no", "n", "off":
        return false
    default:
        return def
    }
}

func failf(format string, a ...any) {
    fmt.Fprintf(os.Stderr, format+"\n", a...)
    os.Exit(1)
}