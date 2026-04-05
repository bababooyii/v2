//! Recursive Polar (Hyperspherical) Decomposition for GTM.
//!
//! Converts a D-dim Cartesian vector into (radius, D-1 angles) representation,
//! enabling independent scalar quantization of each hyperspherical coordinate.

use ndarray::{Array1, ArrayView1};
use std::f64::consts::PI;

const EPS: f64 = 1e-8;

/// Convert Cartesian vector to hyperspherical coordinates.
///
/// Returns:
/// - r: L2 norm (radius)
/// - thetas: D-1 angles in [0, pi] except last in [0, 2*pi)
pub fn polar_transform(v: ArrayView1<f64>) -> (f64, Array1<f64>) {
    let d = v.len();
    let r = v.dot(&v).sqrt().max(EPS);

    // Normalized unit vector
    let v_norm: Array1<f64> = v.mapv(|x| x / r);

    // Compute running squared sums from the end
    let mut running_sq = vec![0.0f64; d];
    let mut cumsum = 0.0f64;
    for i in (0..d).rev() {
        cumsum += v_norm[i] * v_norm[i];
        running_sq[i] = cumsum;
    }

    let mut thetas = Vec::with_capacity(d - 1);

    for i in 0..(d - 1) {
        let numer = v_norm[i];
        let denom = running_sq[i].max(EPS).sqrt();
        let cos_theta = (numer / denom).clamp(-1.0, 1.0);
        let mut theta = cos_theta.acos();

        // Last angle: extend to [0, 2*pi) using sign of last component
        if i == d - 2 {
            let sign = if v_norm[d - 1] < 0.0 { -1.0 } else { 1.0 };
            if sign < 0.0 {
                theta = 2.0 * PI - theta;
            }
        }

        thetas.push(theta);
    }

    (r, Array1::from_vec(thetas))
}

/// Convert hyperspherical coordinates back to Cartesian.
pub fn polar_inverse(r: f64, thetas: &Array1<f64>) -> Array1<f64> {
    let d = thetas.len() + 1;
    let mut coords = Vec::with_capacity(d);
    let mut sin_prod = 1.0f64;

    for i in 0..(d - 1) {
        let theta_i = thetas[i];
        coords.push(sin_prod * theta_i.cos());
        sin_prod *= theta_i.sin();
    }

    // Last coordinate: product of all sines
    coords.push(sin_prod);

    // Scale by radius
    Array1::from_vec(coords).mapv(|x| x * r)
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_abs_diff_eq;
    use ndarray::Array1;

    #[test]
    fn test_polar_norm_preservation() {
        let v: Array1<f64> = Array1::from_vec(vec![3.0, 4.0, 0.0, 12.0]);
        let (r, _) = polar_transform(v.view());
        assert_abs_diff_eq!(r, 13.0, epsilon = 1e-10);
    }

    #[test]
    fn test_polar_round_trip() {
        let v: Array1<f64> = Array1::from_vec(vec![1.0, -2.0, 3.0, -4.0, 5.0]);
        let (r, thetas) = polar_transform(v.view());
        let v_rec = polar_inverse(r, &thetas);

        for i in 0..v.len() {
            assert_abs_diff_eq!(v[i], v_rec[i], epsilon = 1e-10);
        }
    }

    #[test]
    fn test_zero_vector() {
        let v = Array1::<f64>::zeros(16);
        let (r, _) = polar_transform(v.view());
        assert!(r < 1e-6);
    }
}
