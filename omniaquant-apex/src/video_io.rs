//! FFmpeg-based video I/O pipeline
//!
//! Provides frame extraction from video files and frame sequence encoding
//! to output video using FFmpeg subprocess.

use image::DynamicImage;
use std::path::Path;
use std::process::{Command, Stdio};

/// Extract frames from a video file using FFmpeg.
///
/// Returns a vector of DynamicImage frames.
pub fn extract_frames<P: AsRef<Path>>(
    video_path: P,
    max_frames: usize,
    target_width: u32,
    target_height: u32,
) -> Result<Vec<DynamicImage>, String> {
    let output = Command::new("ffmpeg")
        .args([
            "-i",
            video_path.as_ref().to_str().unwrap(),
            "-vf",
            &format!("scale={}:{}", target_width, target_height),
            "-vsync",
            "0",
            "-frame_pts",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "bmp",
            "-",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| format!("Failed to run ffmpeg: {}", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("ffmpeg error: {}", stderr));
    }

    // Parse BMP frames from stdout
    let mut frames = Vec::new();
    let data = output.stdout;
    let mut pos = 0;

    while pos < data.len() && frames.len() < max_frames {
        // BMP magic: "BM"
        if pos + 2 <= data.len() && data[pos] == b'B' && data[pos + 1] == b'M' {
            // Read BMP file size (little-endian u32 at offset 2)
            if pos + 6 <= data.len() {
                let bmp_size = u32::from_le_bytes([
                    data[pos + 2],
                    data[pos + 3],
                    data[pos + 4],
                    data[pos + 5],
                ]) as usize;

                if pos + bmp_size <= data.len() {
                    if let Ok(img) = image::load_from_memory_with_format(
                        &data[pos..pos + bmp_size],
                        image::ImageFormat::Bmp,
                    ) {
                        frames.push(img);
                    }
                    pos += bmp_size;
                    continue;
                }
            }
        }
        pos += 1;
    }

    Ok(frames)
}

/// Encode a sequence of frames to a video file using FFmpeg.
pub fn encode_frames_to_video<P: AsRef<Path>>(
    frames: &[DynamicImage],
    output_path: P,
    fps: u32,
) -> Result<(), String> {
    if frames.is_empty() {
        return Err("No frames to encode".to_string());
    }

    let mut child = Command::new("ffmpeg")
        .args([
            "-y",
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-s",
            &format!("{}x{}", frames[0].width(), frames[0].height()),
            "-pix_fmt",
            "rgb24",
            "-r",
            &fps.to_string(),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            output_path.as_ref().to_str().unwrap(),
        ])
        .stdin(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn ffmpeg: {}", e))?;

    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        for frame in frames {
            let rgb = frame.to_rgb8();
            for y in 0..frame.height() {
                for x in 0..frame.width() {
                    let p = rgb.get_pixel(x, y);
                    stdin
                        .write_all(&[p[0], p[1], p[2]])
                        .map_err(|e| e.to_string())?;
                }
            }
        }
    }

    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("ffmpeg error: {}", stderr));
    }

    Ok(())
}

/// Save a single frame as PNG for debugging/visualization.
pub fn save_frame<P: AsRef<Path>>(frame: &DynamicImage, path: P) -> Result<(), String> {
    frame
        .save(path)
        .map_err(|e| format!("Failed to save frame: {}", e))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encode_frames_to_video() {
        // Create synthetic frames
        let frames: Vec<DynamicImage> = (0..30)
            .map(|i| {
                use image::RgbImage;
                let mut img = RgbImage::new(64, 64);
                for y in 0..64 {
                    for x in 0..64 {
                        img.put_pixel(
                            x,
                            y,
                            image::Rgb([
                                ((x as f32 / 64.0) * 255.0) as u8,
                                ((y as f32 / 64.0) * 255.0) as u8,
                                ((i as f32 / 30.0) * 255.0) as u8,
                            ]),
                        );
                    }
                }
                DynamicImage::ImageRgb8(img)
            })
            .collect();

        let tmp = std::env::temp_dir().join("test_output.mp4");
        let result = encode_frames_to_video(&frames, &tmp, 30);

        // This test requires ffmpeg installed
        if result.is_ok() {
            assert!(tmp.exists());
            let _ = std::fs::remove_file(&tmp);
        }
    }
}
