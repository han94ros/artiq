from numpy import int32, int64
from artiq.language.core import kernel, now_mu, portable, delay
from artiq.coredevice.rtio import rtio_output, rtio_output_wide
from artiq.language.types import TInt32, TInt64, TFloat


class Spline:
    """Spline interpolating RTIO channel.

    One knot of a polynomial basis spline (B-spline) :math:`u(t)`
    is defined by the coefficients :math:`u_n` up to order :math:`n = k`.
    If the knot is evaluated starting at time :math:`t_0`, the output
    :math:`u(t)` for :math:`t > t_0, t_0` is:

    .. math::
        u(t) = \sum_{n=0}^k \frac{u_n}{n!} (t - t_0)^n
        = u_0 + u_1 (t - t_0) + \frac{u_2}{2} (t - t_0)^2 + \dots
    """

    kernel_invariants = {"channel", "core", "scale", "width",
                         "time_width", "time_scale"}

    def __init__(self, width, time_width, channel, core_device, scale=1.):
        self.core = core_device
        self.channel = channel
        self.width = width
        self.scale = float((int64(1) << width) * scale)
        self.time_width = time_width
        self.time_scale = float((1 << time_width) *
                                core_device.coarse_ref_period)

    @portable(flags={"fast-math"})
    def to_mu(self, value: TFloat) -> TInt32:
        """Convert floating point `value` from physical units to 32 bit
        integer machine units."""
        return int32(round(value*self.scale))

    @portable(flags={"fast-math"})
    def from_mu(self, value: TInt32) -> TFloat:
        """Convert 32 bit integer `value` from machine units to floating point
        physical units."""
        return value/self.scale

    @portable(flags={"fast-math"})
    def to_mu64(self, value: TFloat) -> TInt64:
        """Convert floating point `value` from physical units to 64 bit
        integer machine units."""
        return int64(round(value*self.scale))

    @kernel
    def set_mu(self, value: TInt32):
        """Set spline value (machine units).

        :param value: Spline value in integer machine units.
        """
        rtio_output(now_mu(), self.channel, 0, value)

    @kernel(flags={"fast-math"})
    def set(self, value: TFloat):
        """Set spline value.

        :param value: Spline value relative to full-scale.
        """
        if self.width > 32:
            l = [int32(0)] * 2
            self.pack_coeff_mu([self.to_mu64(value)], l)
            rtio_output_wide(now_mu(), self.channel, 0, l)
        else:
            rtio_output(now_mu(), self.channel, 0, self.to_mu(value))

    @kernel
    def set_coeff_mu(self, value):  # TList(TInt32)
        """Set spline raw values.

        :param value: Spline packed raw values.
        """
        rtio_output_wide(now_mu(), self.channel, 0, value)

    @portable(flags={"fast-math"})
    def pack_coeff_mu(self, coeff, packed):  # TList(TInt64), TList(TInt32)
        """Pack coefficients into RTIO data

        :param coeff: TList(TInt64) list of machine units spline coefficients.
            Lowest (zeroth) order first. The coefficient list is zero-extended
            by the RTIO gateware.
        :param packed: TList(TInt32) list for packed RTIO data. Must be
            pre-allocated. Length in bits is
            `n*width + (n - 1)*n//2*time_width`
        """
        pos = 0
        for i in range(len(coeff)):
            wi = self.width + i*self.time_width
            ci = coeff[i]
            while wi != 0:
                j = pos//32
                used = pos - 32*j
                avail = 32 - used
                if avail > wi:
                    avail = wi
                cij = int32(ci)
                if avail != 32:
                    cij &= (1 << avail) - 1
                packed[j] |= cij << used
                ci >>= avail
                wi -= avail
                pos += avail

    @portable(flags={"fast-math"})
    def coeff_to_mu(self, coeff, coeff64):  # TList(TFloat), TList(TInt64)
        """Convert a floating point list of coefficients into a 64 bit
        integer (preallocated).

        :param coeff: TList(TFloat) list of coefficients in physical units.
        :param coeff64: TList(TInt64) preallocated list of coefficients in
            machine units.
        """
        for i in range(len(coeff)):
            vi = coeff[i] * self.scale
            for j in range(i):
                vi *= self.time_scale
            ci = int64(round(vi))
            coeff64[i] = ci
            # artiq.wavesynth.coefficients.discrete_compensate:
            if i == 2:
                coeff64[1] += ci >> self.time_width + 1
            elif i == 3:
                coeff64[2] += ci >> self.time_width
                coeff64[1] += ci // 6 >> 2*self.time_width

    def coeff_as_packed_mu(self, coeff64):
        """Pack 64 bit integer machine units coefficients into 32 bit integer
        RTIO data list.

        This is a host-only method that can be used to generate packed
        spline knot data to be frozen into kernels at compile time.
        """
        n = len(coeff64)
        width = n*self.width + (n - 1)*n//2*self.time_width
        packed = [int32(0)] * ((width + 31)//32)
        self.pack_coeff_mu(coeff64, packed)
        return packed

    def coeff_as_packed(self, coeff):
        """Convert floating point spline coefficients into 32 bit integer
        packed data.

        This is a host-only method that can be used to generate packed
        spline knot data to be frozen into kernels at compile time.
        """
        coeff64 = [int64(0)] * len(coeff)
        self.coeff_to_mu(coeff, coeff64)
        return self.coeff_as_packed_mu(coeff64)

    @kernel(flags={"fast-math"})
    def set_coeff(self, coeff):  # TList(TFloat)
        """Set spline coefficients.

        Missing coefficients (high order) are zero-extended byt the RTIO
        gateware.

        If more coefficients are supplied than the gateware supports the extra
        coefficients are ignored.

        :param value: List of floating point spline knot coefficients,
            lowest order (constant) coefficient first. Units are the
            unit of this spline's value times increasing powers of 1/s.
        """
        n = len(coeff)
        coeff64 = [int64(0)] * n
        self.coeff_to_mu(coeff, coeff64)
        width = n*self.width + (n - 1)*n//2*self.time_width
        packed = [int32(0)] * ((width + 31)//32)
        self.pack_coeff_mu(coeff64, packed)
        self.set_coeff_mu(packed)

    @kernel(flags={"fast-math"})
    def smooth(self, start: TFloat, stop: TFloat, duration: TFloat,
               order: TInt32):
        """Initiate an interpolated value change.

        For zeroth order (step) interpolation, the step is at
        `start + duration/2`.

        First order interpolation corresponds to a linear value ramp from
        `start` to `stop` over `duration`.

        The third order interpolation is constrained to have zero first
        order derivative at both `start` and `stop`.

        For first order and third order interpolation (linear and cubic)
        the interpolator needs to be stopped (or fed a new spline knot)
        explicitly at the stop time.

        This method advances the timeline by `duration`.

        :param start: Initial value of the change. In physical units.
        :param stop: Final value of the change. In physical units.
        :param duration: Duration of the interpolation. In physical units.
        :param order: Order of the interpolation. Only 0, 1,
            and 3 are valid: step, linear, cubic.
        """
        if order == 0:
            delay(duration/2.)
            self.set_coeff([stop])
            delay(duration/2.)
        elif order == 1:
            self.set_coeff([start, (stop - start)/duration])
            delay(duration)
        elif order == 3:
            v2 = 6.*(stop - start)/(duration*duration)
            self.set_coeff([start, 0., v2, -2.*v2/duration])
            delay(duration)
        else:
            raise ValueError("Invalid interpolation order. "
                             "Supported orders are: 0, 1, 3.")


class SAWG:
    """Smart arbitrary waveform generator channel.
    The channel is parametrized as: ::

        oscillators = exp(2j*pi*(frequency0*t + phase0))*(
            amplitude1*exp(2j*pi*(frequency1*t + phase1)) +
            amplitude2*exp(2j*pi*(frequency2*t + phase2)))

        output = (offset +
            i_enable*Re(oscillators) +
            q_enable*Im(buddy_oscillators))

    Where:
        * offset, amplitude1, amplitude2: in units of full scale
        * phase0, phase1, phase2: in units of turns
        * frequency0, frequency1, frequency2: in units of Hz

    :param channel_base: RTIO channel number of the first channel (amplitude).
        Frequency and Phase are then assumed to be successive channels.
    """
    kernel_invariants = {"channel_base", "core",
                         "amplitude1", "frequency1", "phase1",
                         "amplitude2", "frequency2", "phase2",
                         "frequency0", "phase0", "offset"}

    def __init__(self, dmgr, channel_base, parallelism, core_device="core"):
        self.core = dmgr.get(core_device)
        self.channel_base = channel_base
        width = 16
        time_width = 16
        cordic_gain = 1.646760258057163  # Cordic(width=16, guard=None).gain
        # cfg: channel_base
        self.offset = Spline(width, time_width, channel_base + 1,
                             self.core, 1/2)
        self.amplitude1 = Spline(width, time_width, channel_base + 2,
                                 self.core, 1/(2*cordic_gain**2))
        self.frequency1 = Spline(3*width, time_width, channel_base + 3,
                                 self.core, self.core.coarse_ref_period)
        self.phase1 = Spline(width, time_width, channel_base + 4,
                             self.core, 1.)
        self.amplitude2 = Spline(width, time_width, channel_base + 5,
                                 self.core, 1/(2*cordic_gain**2))
        self.frequency2 = Spline(3*width, time_width, channel_base + 6,
                                 self.core, self.core.coarse_ref_period)
        self.phase2 = Spline(width, time_width, channel_base + 7,
                             self.core, 1.)
        self.frequency0 = Spline(2*width, time_width, channel_base + 8,
                                 self.core,
                                 self.core.coarse_ref_period/parallelism)
        self.phase0 = Spline(width, time_width, channel_base + 9,
                             self.core, 1.)
