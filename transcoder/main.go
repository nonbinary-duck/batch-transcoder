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
    "path/filepath"
    "sort"
    "strconv"
    "strings"
    "sync"
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
    defaultFFmpegImage = "lscr.io/linuxserver/ffmpeg:latest"
)

type AppConfig struct {
    InputDir      string
    OutputDir     string
    FFmpegImage   string
    Concurrency   int
    PullIfMissing bool
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
    Key      string
    OutPath  string
    Seconds  float64
    Speed    string
    Done     bool
    Err      error
    Duration float64
}

type RunningState struct {
    mu        sync.Mutex
    activeSec map[string]float64
    activeSpd map[string]string
    completed float64
    total     float64
    doneCount int
    wantCount int
    started   time.Time
}

func main() {
    cfg := AppConfig{
        InputDir:      getenv("INPUT_DIR", defaultInputRoot),
        OutputDir:     getenv("OUTPUT_DIR", defaultOutputRoot),
        FFmpegImage:   getenv("FFMPEG_IMAGE", defaultFFmpegImage),
        Concurrency:   getenvInt("JOBS", defaultJobs),
        PullIfMissing: true,
    }

    if cfg.Concurrency < 1 {
        cfg.Concurrency = 1
    }

    if err := os.MkdirAll(cfg.OutputDir, 0o755); err != nil {
        failf("create output dir: %v", err)
    }

    cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
    if err != nil {
        failf("docker client init: %v", err)
    }

    ctx := context.Background()

    if cfg.PullIfMissing {
        if err := ensureImage(ctx, cli, cfg.FFmpegImage); err != nil {
            failf("ensure ffmpeg image: %v", err)
        }
    }

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
        activeSec: make(map[string]float64),
        activeSpd: make(map[string]string),
        started:   time.Now(),
    }
    for _, j := range jobs {
        state.total += j.DurationSec
        state.wantCount++
        if j.Needs1080 {
            state.total += j.DurationSec
            state.wantCount++
        }
    }

    fmt.Printf("Queued %d source file(s)\n", len(jobs))
    for _, j := range jobs {
        fmt.Printf("- %s [%dx%d HDR=%v DV=%v 1080=%v]\n",
            filepath.Base(j.InputPath), j.Width, j.Height, j.IsHDR, j.IsDV, j.Needs1080)
        fmt.Printf("  native: %s\n", j.NativeOut)
        if j.Needs1080 {
            fmt.Printf("  1080p : %s\n", j.Out1080)
        }
    }

    progressCh := make(chan ProgressEvent, 256)
    jobCh := make(chan Job)

    var wg sync.WaitGroup
    for i := 0; i < cfg.Concurrency; i++ {
        wg.Add(1)
        go func() {
            defer wg.Done()
            for job := range jobCh {
                processJob(ctx, cli, cfg, job, progressCh)
            }
        }()
    }

    go func() {
        for _, j := range jobs {
            jobCh <- j
        }
        close(jobCh)
        wg.Wait()
        close(progressCh)
    }()

    ticker := time.NewTicker(1 * time.Second)
    defer ticker.Stop()

    for {
        select {
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

        hostInput := filepath.Join(cfg.InputDir, e.Name())
        meta, err := probeFile(ctx, cli, cfg, hostInput)
        if err != nil {
            fmt.Fprintf(os.Stderr, "probe failed for %s: %v\n", hostInput, err)
            continue
        }

        v, dur, err := selectVideo(meta)
        if err != nil {
            fmt.Fprintf(os.Stderr, "skip %s: %v\n", hostInput, err)
            continue
        }

        isDV := hasDV(v)
        isHDR := isHDR(v) || isDV
        needs1080 := isHDR || v.Width >= 3840 || v.Height >= 2160

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
            InputPath:   hostInput,
            BaseName:    base,
            DurationSec: dur,
            Width:       v.Width,
            Height:      v.Height,
            IsHDR:       isHDR,
            IsDV:        isDV,
            Needs1080:   needs1080,
            NativeOut:   native,
            Out1080:     out1080,
        })
    }
    return jobs, nil
}

func processJob(ctx context.Context, cli *client.Client, cfg AppConfig, job Job, progressCh chan<- ProgressEvent) {
    if !exists(job.NativeOut) {
        if err := runFFmpeg(ctx, cli, cfg, job, false, progressCh); err != nil {
            progressCh <- ProgressEvent{
                Key:      labelFor(job, false),
                OutPath:  job.NativeOut,
                Err:      err,
                Done:     true,
                Duration: job.DurationSec,
            }
        }
    } else {
        progressCh <- ProgressEvent{
            Key:      labelFor(job, false),
            OutPath:  job.NativeOut,
            Seconds:  job.DurationSec,
            Done:     true,
            Duration: job.DurationSec,
        }
    }

    if job.Needs1080 {
        if !exists(job.Out1080) {
            if err := runFFmpeg(ctx, cli, cfg, job, true, progressCh); err != nil {
                progressCh <- ProgressEvent{
                    Key:      labelFor(job, true),
                    OutPath:  job.Out1080,
                    Err:      err,
                    Done:     true,
                    Duration: job.DurationSec,
                }
            }
        } else {
            progressCh <- ProgressEvent{
                Key:      labelFor(job, true),
                OutPath:  job.Out1080,
                Seconds:  job.DurationSec,
                Done:     true,
                Duration: job.DurationSec,
            }
        }
    }
}

func runFFmpeg(ctx context.Context, cli *client.Client, cfg AppConfig, job Job, make1080 bool, progressCh chan<- ProgressEvent) error {
    hostInput := job.InputPath
    hostOutput := job.NativeOut
    if make1080 {
        hostOutput = job.Out1080
    }

    inDir := filepath.Dir(hostInput)
    inFile := filepath.Base(hostInput)
    outDir := filepath.Dir(hostOutput)
    outFile := filepath.Base(hostOutput)

    containerInputPath := filepath.Join("/work/input", inFile)
    containerOutputPath := filepath.Join("/work/output", outFile)

    vf := buildFilter(job, make1080)
    x265 := buildX265Params()

    ffCmd := []string{
        "-hide_banner",
        "-nostdin",
        "-y",
        "-progress", "pipe:1",
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
            Image:        cfg.FFmpegImage,
            Cmd:          ffCmd,
            Tty:          false,
            AttachStdout: true,
            AttachStderr: true,
        },
        HostConfig: &container.HostConfig{
            AutoRemove: true,
            Mounts: []mount.Mount{
                {Type: mount.TypeBind, Source: inDir, Target: "/work/input", ReadOnly: true},
                {Type: mount.TypeBind, Source: outDir, Target: "/work/output", ReadOnly: false},
            },
            NetworkMode: "none",
            SecurityOpt: []string{"no-new-privileges:true"},
        },
        NetworkingConfig: &network.NetworkingConfig{},
    })
    if err != nil {
        return fmt.Errorf("container create: %w", err)
    }

    attachRes, err := cli.ContainerAttach(ctx, createRes.ID, client.ContainerAttachOptions{
        Stream: true,
        Stdout: true,
        Stderr: true,
        Logs:   true,
    })
    if err != nil {
        _, _ = cli.ContainerRemove(context.Background(), createRes.ID, client.ContainerRemoveOptions{Force: true})
        return fmt.Errorf("attach: %w", err)
    }
    defer attachRes.Close()

    if _, err := cli.ContainerStart(ctx, createRes.ID, client.ContainerStartOptions{}); err != nil {
        _, _ = cli.ContainerRemove(context.Background(), createRes.ID, client.ContainerRemoveOptions{Force: true})
        return fmt.Errorf("container start: %w", err)
    }

    doneLogs := make(chan struct{})
    go func() {
        defer close(doneLogs)
        stdoutR, _ := demuxAttach(attachRes.Reader)
        parseFFmpegProgress(stdoutR, labelFor(job, make1080), hostOutput, job.DurationSec, progressCh)
    }()

    waitRes := cli.ContainerWait(ctx, createRes.ID, client.ContainerWaitOptions{
        Condition: container.WaitConditionNotRunning,
    })
    select {
    case err := <-waitRes.Error:
        <-doneLogs
        if err != nil {
            return fmt.Errorf("container wait error: %w", err)
        }
    case res := <-waitRes.Result:
        <-doneLogs
        if res.StatusCode != 0 {
            return fmt.Errorf("ffmpeg exited with status %d", res.StatusCode)
        }
    }

    progressCh <- ProgressEvent{
        Key:      labelFor(job, make1080),
        OutPath:  hostOutput,
        Seconds:  job.DurationSec,
        Done:     true,
        Duration: job.DurationSec,
    }
    return nil
}

func probeFile(ctx context.Context, cli *client.Client, cfg AppConfig, hostInput string) (*FFProbe, error) {
    inDir := filepath.Dir(hostInput)
    inFile := filepath.Base(hostInput)
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
            AutoRemove: true,
            Mounts: []mount.Mount{
                {Type: mount.TypeBind, Source: inDir, Target: "/work/input", ReadOnly: true},
            },
            NetworkMode: "none",
            SecurityOpt: []string{"no-new-privileges:true"},
        },
        NetworkingConfig: &network.NetworkingConfig{},
    })
    if err != nil {
        return nil, err
    }

    attachRes, err := cli.ContainerAttach(ctx, createRes.ID, client.ContainerAttachOptions{
        Stream: true,
        Stdout: true,
        Stderr: true,
        Logs:   true,
    })
    if err != nil {
        _, _ = cli.ContainerRemove(context.Background(), createRes.ID, client.ContainerRemoveOptions{Force: true})
        return nil, err
    }
    defer attachRes.Close()

    if _, err := cli.ContainerStart(ctx, createRes.ID, client.ContainerStartOptions{}); err != nil {
        _, _ = cli.ContainerRemove(context.Background(), createRes.ID, client.ContainerRemoveOptions{Force: true})
        return nil, err
    }

    var stdout bytes.Buffer
    var stderr bytes.Buffer
    _, err = stdcopy.StdCopy(&stdout, &stderr, attachRes.Reader)
    if err != nil {
        return nil, err
    }

    waitRes := cli.ContainerWait(ctx, createRes.ID, client.ContainerWaitOptions{
        Condition: container.WaitConditionNotRunning,
    })
    select {
    case err := <-waitRes.Error:
        if err != nil {
            return nil, err
        }
    case res := <-waitRes.Result:
        if res.StatusCode != 0 {
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

    rc, err := cli.ImagePull(ctx, ref, client.ImagePullOptions{})
    if err != nil {
        return err
    }
    defer rc.Close()
    _, _ = io.Copy(io.Discard, rc)
    return nil
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

func demuxAttach(r io.Reader) (io.Reader, io.Reader) {
    stdoutPr, stdoutPw := io.Pipe()
    stderrPr, stderrPw := io.Pipe()

    go func() {
        defer stdoutPw.Close()
        defer stderrPw.Close()
        _, err := stdcopy.StdCopy(stdoutPw, stderrPw, r)
        if err != nil {
            _ = stdoutPw.CloseWithError(err)
            _ = stderrPw.CloseWithError(err)
            return
        }
    }()

    return stdoutPr, stderrPr
}

func parseFFmpegProgress(stdout io.Reader, key, outPath string, duration float64, ch chan<- ProgressEvent) {
    sc := bufio.NewScanner(stdout)
    var speed string
    for sc.Scan() {
        line := strings.TrimSpace(sc.Text())
        if line == "" {
            continue
        }
        if strings.HasPrefix(line, "out_time_ms=") {
            v := strings.TrimPrefix(line, "out_time_ms=")
            us, _ := strconv.ParseFloat(v, 64)
            ch <- ProgressEvent{
                Key:      key,
                OutPath:  outPath,
                Seconds:  us / 1000000.0,
                Speed:    speed,
                Duration: duration,
            }
        } else if strings.HasPrefix(line, "speed=") {
            speed = strings.TrimSpace(strings.TrimPrefix(line, "speed="))
        }
    }
}

func applyProgress(state *RunningState, ev ProgressEvent) {
    state.mu.Lock()
    defer state.mu.Unlock()

    if ev.Done {
        delete(state.activeSec, ev.Key)
        delete(state.activeSpd, ev.Key)
        state.completed += ev.Duration
        state.doneCount++
        if ev.Err != nil {
            fmt.Printf("\nERROR: %s: %v\n", ev.Key, ev.Err)
        } else {
            fmt.Printf("\nDONE: %s -> %s\n", ev.Key, ev.OutPath)
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

    activeSum := 0.0
    for _, v := range state.activeSec {
        activeSum += v
    }
    done := state.completed + activeSum

    pct := 0.0
    if state.total > 0 {
        pct = done / state.total * 100
    }
    pct = math.Min(pct, 100.0)

    elapsed := time.Since(state.started)
    eta := time.Duration(0)
    if done > 0 && !final {
        totalEstimate := elapsed.Seconds() * (state.total / done)
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

func failf(format string, a ...any) {
    fmt.Fprintf(os.Stderr, format+"\n", a...)
    os.Exit(1)
}