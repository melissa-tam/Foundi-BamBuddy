import { describe, it, expect } from 'vitest';
import { validateForm, newSpoolFormData } from '../../components/spool-form/types';

describe('validateForm', () => {
  describe('standard mode', () => {
    it('requires slicer_filament, material, brand, and subtype', () => {
      const result = validateForm(newSpoolFormData(1000));
      expect(result.isValid).toBe(false);
      expect(result.errors.slicer_filament).toBeDefined();
      expect(result.errors.material).toBeDefined();
      expect(result.errors.brand).toBeDefined();
      expect(result.errors.subtype).toBeDefined();
    });

    it('passes when all required fields are filled', () => {
      const data = {
        ...newSpoolFormData(1000),
        slicer_filament: 'Bambu PLA Basic @BBL',
        material: 'PLA',
        brand: 'Bambu Lab',
        subtype: 'Basic',
      };
      const result = validateForm(data);
      expect(result.isValid).toBe(true);
      expect(Object.keys(result.errors)).toHaveLength(0);
    });
  });

  describe('quickAdd mode', () => {
    it('only requires material', () => {
      const result = validateForm(newSpoolFormData(1000), true);
      expect(result.isValid).toBe(false);
      expect(result.errors.material).toBeDefined();
      expect(result.errors.slicer_filament).toBeUndefined();
      expect(result.errors.brand).toBeUndefined();
    });

    it('passes with only material set', () => {
      const data = { ...newSpoolFormData(1000), material: 'PETG' };
      const result = validateForm(data, true);
      expect(result.isValid).toBe(true);
    });
  });

  describe('spoolmanMode', () => {
    it('only requires material (same as quickAdd)', () => {
      const result = validateForm(newSpoolFormData(1000), false, true);
      expect(result.isValid).toBe(false);
      expect(result.errors.material).toBeDefined();
      expect(result.errors.slicer_filament).toBeUndefined();
      expect(result.errors.brand).toBeUndefined();
      expect(result.errors.subtype).toBeUndefined();
    });

    it('passes with only material set', () => {
      const data = { ...newSpoolFormData(1000), material: 'PLA' };
      const result = validateForm(data, false, true);
      expect(result.isValid).toBe(true);
      expect(Object.keys(result.errors)).toHaveLength(0);
    });

    it('does not require slicer_filament even when present', () => {
      const data = { ...newSpoolFormData(1000), material: 'ABS' };
      const result = validateForm(data, false, true);
      expect(result.isValid).toBe(true);
    });

    it('fails when material is empty string', () => {
      const data = { ...newSpoolFormData(1000), material: '' };
      const result = validateForm(data, false, true);
      expect(result.isValid).toBe(false);
      expect(result.errors.material).toBeDefined();
    });

    it('quickAdd takes precedence over spoolmanMode', () => {
      const data = { ...newSpoolFormData(1000), material: 'PLA' };
      const result = validateForm(data, true, true);
      expect(result.isValid).toBe(true);
    });

    it('passes when spoolman_filament_id is set and material is empty', () => {
      // When a catalog entry is pre-selected, material is not required
      const data = { ...newSpoolFormData(1000), material: '', spoolman_filament_id: 7 };
      const result = validateForm(data, false, true);
      expect(result.isValid).toBe(true);
      expect(result.errors.material).toBeUndefined();
    });

    it('fails when both material and spoolman_filament_id are absent', () => {
      const data = { ...newSpoolFormData(1000), material: '', spoolman_filament_id: null };
      const result = validateForm(data, false, true);
      expect(result.isValid).toBe(false);
      expect(result.errors.material).toBeDefined();
    });
  });
});
