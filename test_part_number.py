"""Sanity checks for part-number normalization."""
import unittest

from app import (
    credit_part_numbers,
    normalize_part_number,
    parse_shipper,
    parts_match,
)


class TestNormalizePartNumber(unittest.TestCase):
    def test_ocr_i_becomes_one_and_keeps_leading_u(self):
        norm, flagged = normalize_part_number('U8i56209AH')
        self.assertEqual(norm, 'U8156209AH')
        self.assertNotEqual(norm, 'U856209AH')
        self.assertFalse(flagged)

    def test_already_valid_unchanged(self):
        norm, flagged = normalize_part_number('U8453637AB')
        self.assertEqual(norm, 'U8453637AB')
        self.assertFalse(flagged)

    def test_no_leading_u(self):
        norm, flagged = normalize_part_number('8453637ab')
        self.assertEqual(norm, '8453637AB')
        self.assertFalse(flagged)

    def test_invalid_is_flagged_not_dropped(self):
        norm, flagged = normalize_part_number('U12AB')
        self.assertEqual(norm, 'U12AB')
        self.assertTrue(flagged)

    def test_shipper_and_credit_compare_normalized_forms(self):
        ship = parse_shipper(
            'C112345678 554433 U8i56209AH ALTERNATOR 1 25.00\n')
        self.assertEqual(len(ship), 1)
        self.assertEqual(ship[0]['part'], 'U8156209AH')
        self.assertFalse(ship[0]['part_flagged'])

        credit = (
            'CREDIT MEMO NUMBER: 03181000CC100\n'
            'REFERENCE/CONTROL NUMBER C112345678 U8I56209AH\n'
        )
        parts = credit_part_numbers(credit)
        self.assertEqual(parts['C112345678'][0], 'U8156209AH')
        self.assertFalse(parts['C112345678'][1])
        self.assertTrue(parts_match(ship[0]['part'], 'U8i56209AH'))
        self.assertTrue(parts_match('U8i56209AH', parts['C112345678'][0]))


if __name__ == '__main__':
    unittest.main()
