/* Stands in for libairspy in end-to-end tests: the satellite only opens an
 * Airspy to turn its AGCs off (sdr._airspy_agc_off). */
#include <stdint.h>
int airspy_open_sn(void **dev, uint64_t sn) { (void)sn; *dev = (void *)1; return 0; }
int airspy_set_lna_agc(void *dev, uint8_t v) { (void)dev; (void)v; return 0; }
int airspy_set_mixer_agc(void *dev, uint8_t v) { (void)dev; (void)v; return 0; }
int airspy_close(void *dev) { (void)dev; return 0; }
