# Copyright 2016, FBPIC contributors
# Authors: Remi Lehe, Manuel Kirchen
# License: 3-Clause-BSD-LBNL
"""
This file is part of the Fourier-Bessel Particle-In-Cell code (FB-PIC)
It defines the structure necessary to implement the moving window.
"""
import numpy as np
from scipy.constants import c
from fbpic.particles import Particles
from fbpic.lpa_utils.boosted_frame import BoostConverter
# Check if CUDA is available, then import CUDA functions
from fbpic.cuda_utils import cuda_installed
if cuda_installed:
    from fbpic.cuda_utils import cuda, cuda_tpb_bpg_2d

class MovingWindow(object):
    """
    Class that contains the moving window's variables and methods
    """
    def __init__( self, interp, comm, dt, ptcl, v, p_nz, time,
                  ux_m=0., uy_m=0., uz_m=0., ux_th=0., uy_th=0., uz_th=0.,
                  gamma_boost=None ) :
        """
        Initializes a moving window object.

        Parameters
        ----------
        interp: a list of Interpolation objects
            Contains the positions of the boundaries

        comm: a BoundaryCommunicator object
            Contains information about the MPI decomposition
            and about the longitudinal boundaries

        dt: float
            The timestep of the simulation.

        ptcl: a list of Particle objects
            Needed in order to infer the position of injection
            of the particles by the moving window.

        v: float (meters per seconds), optional
            The speed of the moving window

        p_nz: int
            Number of macroparticles per cell along the z direction

        time: float (seconds)
            The time (in the simulation) at which the moving
            window was initialized

        ux_m, uy_m, uz_m: floats (dimensionless)
           Normalized mean momenta of the injected particles in each direction

        ux_th, uy_th, uz_th: floats (dimensionless)
           Normalized thermal momenta in each direction

        gamma_boost : float, optional
            When initializing the laser in a boosted frame, set the
            value of `gamma_boost` to the corresponding Lorentz factor.
            (uz_m is to be given in the lab frame ; for the moment, this
            will not work if any of ux_th, uy_th, uz_th, ux_m, uy_m is nonzero)
        """
        # Check that the boundaries are open
        if ((comm.rank == comm.size-1) and (comm.right_proc is not None)) \
          or ((comm.rank == 0) and (comm.left_proc is not None)):
          raise ValueError('The simulation is using a moving window, but '
                    'the boundaries are periodic.\n Please select open '
                    'boundaries when initializing the Simulation object.')

        # Momenta parameters
        self.ux_m = ux_m
        self.uy_m = uy_m
        self.uz_m = uz_m
        self.ux_th = ux_th
        self.uy_th = uy_th
        self.uz_th = uz_th

        # When running the simulation in boosted frame, convert the arguments
        if gamma_boost is not None:
            boost = BoostConverter( gamma_boost )
            self.uz_m, = boost.longitudinal_momentum([ self.uz_m ])

        # Attach moving window speed and period
        self.v = v

        # Attach reference position of moving window (only for the first proc)
        # (Determines by how many cells the window should be moved)
        if comm.rank == 0:
            self.zmin = interp[0].zmin

        # Attach injection position and speed (only for the last proc)
        if comm.rank == comm.size-1:
            self.v_end_plasma = \
                c * self.uz_m / np.sqrt(1 + ux_m**2 + uy_m**2 + self.uz_m**2)
            ng = comm.n_guard
            nd = comm.n_damp
            # Initialize plasma *ahead* of the right *physical* boundary of
            # the box so, after `exchange_period` iterations
            # (without adding new plasma), there will still be plasma
            # inside the physical domain.
            self.z_inject = interp[0].zmax - (ng+nd)*interp[0].dz + \
                comm.exchange_period * (v-self.v_end_plasma) * dt
            # Try to detect the position of the end of the plasma:
            # Find the maximal position of the particles which are
            # continously injected.
            self.z_end_plasma = None
            for species in ptcl:
                if species.continuous_injection and species.Ntot != 0:
                    # Add half of the spacing between particles (the injection
                    # function itself will add a half-spacing again)
                    self.z_end_plasma = species.z.max() + 0.5*interp[0].dz/p_nz
                    break
            # Default value in the absence of continuously-injected particles
            if self.z_end_plasma is None:
                self.z_end_plasma = self.z_inject
            self.nz_inject = 0
            self.p_nz = p_nz

        # Attach time of last move
        self.t_last_move = time - dt

    def move_grids(self, fld, comm, time, shift_spectral=True):
        """
        Calculate by how many cells the moving window should be moved.
        If this is non-zero, shift the fields on the interpolation grid,
        and add new particles.

        NB: the spectral grid is not modified, as it is automatically
        updated after damping the fields (see main.py)

        Parameters
        ----------
        fld: a Fields object
            Contains the fields data of the simulation

        comm: an fbpic BoundaryCommunicator object
            Contains the information on the MPI decomposition

        time: float (seconds)
            The global time in the simulation
            This is used in order to determine how much the window should move

        shift_spectral: bool (optional)
            Whether to shift the fields in spectral space (spectral grid)
            or in spatial space (interpolation grid).
            Defaults to True, as shifting the fields in spectral space is
            much more efficient.
        """
        # To avoid discrepancies between processors, only the first proc
        # decides whether to send the data, and broadcasts the information.
        dz = fld.interp[0].dz
        if comm.rank==0:
            # Move the continuous position of the moving window object
            self.zmin += self.v * (time - self.t_last_move)
            # Find the number of cells by which the window should move
            n_move = int( (self.zmin - fld.interp[0].zmin)/dz )
        else:
            n_move = None
        # Broadcast the information to all proc
        if comm.size > 1:
            n_move = comm.mpi_comm.bcast( n_move )

        # Move the grids
        if n_move != 0:
            # Shift the fields
            Nm = len(fld.interp)
            for m in range(Nm):
                # Modify the values of the corresponding z's
                fld.interp[m].z += n_move*fld.interp[m].dz
                fld.interp[m].zmin += n_move*fld.interp[m].dz
                fld.interp[m].zmax += n_move*fld.interp[m].dz

                if shift_spectral:
                    # Shift/move fields by n_move cells in spectral space
                    self.shift_spect_grid( fld.spect[m], n_move )
                else:
                    # Shift/move fields by n_move cells in spatial space
                    self.shift_interp_grid( fld.interp[m], n_move)

        # Because the grids have just been shifted, there is a shift
        # in the cell indices that are used for the prefix sum.
        if fld.use_cuda:
            fld.prefix_sum_shift += n_move
            # This quantity is reset to 0 whenever prefix_sum is recalculated

        # Prepare the positions of injection for the particles
        # (The actual creation of particles is done when the routine
        # exchange_particles of boundary_communicator.py is called)
        if comm.rank == comm.size-1:
            # Move the injection position
            self.z_inject += self.v * (time - self.t_last_move)
            # Take into account the motion of the end of the plasma
            self.z_end_plasma += self.v_end_plasma * (time - self.t_last_move)
            # Increment the number of particle cells to add
            nz_new = int( (self.z_inject - self.z_end_plasma)/dz )
            self.nz_inject += nz_new
            # Increment the virtual position of the end of the plasma
            # (When `generate_particles` is called, then the plasma
            # is injected between z_end_plasma - nz_inject*dz and z_end_plasma,
            # and afterwards nz_inject is set to 0.)
            self.z_end_plasma += nz_new*dz

        # Change the time of the last move
        self.t_last_move = time

    def generate_particles( self, species, dz, time ) :
        """
        Generate new particles at the right end of the plasma
        (i.e. between z_end_plasma - nz_inject*dz and z_end_plasma)

        Return them in the form of a particle buffer of shape (8, Nptcl)

        Parameters
        ----------
        species: a Particles object
            Contains data about the existing particles

        dz: float (meters)
            The grid spacing along see on the grid

        time: float (seconds)
            The global time of the simulation
            (Needed in order to infer how much the plasma has moved)

        Returns
        -------
        - float_buffer: An array of floats of shape (n_float, Nptcl)
            that contain the float properties of the particles
        - uint_buffer: An array of uints of shape (n_int, Nptcl)
            that contain the integer properties of the particles (e.g. id)
        """
        # Shortcut for the number of integer quantities
        n_int = species.n_integer_quantities
        n_float = species.n_float_quantities

        # Create new particle cells
        if (self.nz_inject > 0) and (species.continuous_injection == True):
            # Create a temporary density function that takes into
            # account the fact that the plasma has moved
            if species.dens_func is not None:
                def dens_func( z, r ):
                    return( species.dens_func( z-self.v_end_plasma*time, r ) )
            else:
                dens_func = None
            # Create the particles that will be added
            zmax = self.z_end_plasma
            zmin = self.z_end_plasma - self.nz_inject*dz
            Npz = self.nz_inject * self.p_nz
            new_ptcl = Particles( species.q, species.m, species.n,
                Npz, zmin, zmax, species.Npr, species.rmin, species.rmax,
                species.Nptheta, species.dt, dens_func=dens_func,
                ux_m=self.ux_m, uy_m=self.uy_m, uz_m=self.uz_m,
                ux_th=self.ux_th, uy_th=self.uy_th, uz_th=self.uz_th)

            # Initialize ionization-relevant arrays if species is ionizable
            if species.ionizer is not None:
                new_ptcl.make_ionizable( element=species.ionizer.element,
                    target_species=species.ionizer.target_species,
                    level_start=species.ionizer.level_start,
                    full_initialization=False )
            # Convert them to a particle buffer
            # - Float buffer
            float_buffer = np.empty( (n_float, new_ptcl.Ntot), dtype=np.float64 )
            float_buffer[0,:] = new_ptcl.x
            float_buffer[1,:] = new_ptcl.y
            float_buffer[2,:] = new_ptcl.z
            float_buffer[3,:] = new_ptcl.ux
            float_buffer[4,:] = new_ptcl.uy
            float_buffer[5,:] = new_ptcl.uz
            float_buffer[6,:] = new_ptcl.inv_gamma
            float_buffer[7,:] = new_ptcl.w
            if species.ionizer is not None:
                float_buffer[8,:] = new_ptcl.ionizer.neutral_weight
            # - Integer buffer
            uint_buffer = np.empty( (n_int, new_ptcl.Ntot), dtype=np.uint64 )
            i_int = 0
            if species.tracker is not None:
                uint_buffer[i_int,:] = \
                    species.tracker.generate_new_ids(new_ptcl.Ntot)
                i_int += 1
            if species.ionizer is not None:
                uint_buffer[i_int,:] = new_ptcl.ionizer.ionization_level
        else:
            # No new particles: initialize empty arrays
            float_buffer = np.empty( (n_float, 0), dtype=np.float64 )
            uint_buffer = np.empty( (n_int, 0), dtype=np.uint64 )

        return( float_buffer, uint_buffer )

    def shift_spect_grid( self, grid, n_move,
                          shift_rho=True, shift_currents=True ):
        """
        Shift the spectral fields by n_move cells (with respect to the
        spatial grid). Shifting is done either on the CPU or the GPU,
        if use_cuda is True. (Typically n_move is positive, and the
        fields are shifted backwards)

        Parameters
        ----------
        grid: an SpectralGrid corresponding to one given azimuthal mode
            Contains the values of the fields in spectral space,
            and is modified by this function.

        n_move: int
            The number of cells by which the grid should be shifted

        shift_rho: bool, optional
            Whether to also shift the charge density
            Default: True, since rho is only recalculated from
            scratch when the particles are exchanged

        shift_currents: bool, optional
            Whether to also shift the currents
            Default: False, since the currents are recalculated from
            scratch at each PIC cycle
        """
        if grid.use_cuda:
            shift = grid.d_field_shift
            # Shift all the fields on the GPU
            self.shift_spect_field_gpu( grid.Ep, shift, n_move )
            self.shift_spect_field_gpu( grid.Em, shift, n_move )
            self.shift_spect_field_gpu( grid.Ez, shift, n_move )
            self.shift_spect_field_gpu( grid.Bp, shift, n_move )
            self.shift_spect_field_gpu( grid.Bm, shift, n_move )
            self.shift_spect_field_gpu( grid.Bz, shift, n_move )
            if shift_rho:
                self.shift_spect_field_gpu( grid.rho_prev, shift, n_move )
            if shift_currents:
                self.shift_spect_field_gpu( grid.Jp, shift, n_move )
                self.shift_spect_field_gpu( grid.Jm, shift, n_move )
                self.shift_spect_field_gpu( grid.Jz, shift, n_move )
        else:
            shift = grid.field_shift
            # Shift all the fields on the CPU
            self.shift_spect_field( grid.Ep, shift, n_move )
            self.shift_spect_field( grid.Em, shift, n_move )
            self.shift_spect_field( grid.Ez, shift, n_move )
            self.shift_spect_field( grid.Bp, shift, n_move )
            self.shift_spect_field( grid.Bm, shift, n_move )
            self.shift_spect_field( grid.Bz, shift, n_move )
            if shift_rho:
                self.shift_spect_field( grid.rho_prev, shift, n_move )
            if shift_currents:
                self.shift_spect_field( grid.Jp, shift, n_move )
                self.shift_spect_field( grid.Jm, shift, n_move )
                self.shift_spect_field( grid.Jz, shift, n_move )

    def shift_spect_field( self, field_array, shift_factor, n_move ):
        """
        Shift the field 'field_array' by n_move cells.
        This is done in spectral space and corresponds to multiplying the
        fields with the factor exp(i*kz_true*dz)**n_move .
        (Typically n_move is positive, and the fields are shifted backwards)

        Parameters
        ----------
        field_array: 2darray of complexs
            Contains the value of the fields, and is modified by
            this function

        shift_factor: 1darray of complexs
            Contains the shift array, that is multiplied to the fields in
            spectral space to shift them by one cell in spatial space
            ( exp(i*kz_true*dz) )

        n_move: int
            The number of cells by which the grid should be shifted
        """
        # Multiply with (shift_factor*sign(n_move))**n_move
        field_array *= ( shift_factor[:, np.newaxis] )**n_move

    def shift_spect_field_gpu( self, field_array, shift_factor, n_move):
        """
        Shift the field 'field_array' by n_move cells on the GPU.
        This is done in spectral space and corresponds to multiplying the
        fields with the factor exp(i*kz_true*dz)**n_move .
        (Typically n_move is positive, and the fields are shifted backwards)

        Parameters
        ----------
        field_array: 2darray of complexs
            Contains the value of the fields, and is modified by
            this function

        shift_factor: 1darray of complexs
            Contains the shift array, that is multiplied to the fields in
            spectral space to shift them by one cell in spatial space
            ( exp(i*kz_true*dz) )

        n_move: int
            The number of cells by which the grid should be shifted
        """
        # Get a 2D CUDA grid of the size of the grid
        dim_grid_2d, dim_block_2d = cuda_tpb_bpg_2d(
            field_array.shape[0], field_array.shape[1] )
        # Shift the field array in place
        shift_spect_array_gpu[dim_grid_2d, dim_block_2d](
            field_array, shift_factor, n_move)

    def shift_interp_grid( self, grid, n_move,
                           shift_rho=True, shift_currents=False ):
        """
        Shift the interpolation grid by n_move cells. Shifting is done
        either on the CPU or the GPU, if use_cuda is True.
        (Typically n_move is positive, and the fields are shifted backwards)

        Parameters
        ----------
        grid: an InterpolationGrid corresponding to one given azimuthal mode
            Contains the values of the fields on the interpolation grid,
            and is modified by this function.

        n_move: int
            The number of cells by which the grid should be shifted

        shift_rho: bool, optional
            Whether to also shift the charge density
            Default: True, since rho is only recalculated from
            scratch when the particles are exchanged

        shift_currents: bool, optional
            Whether to also shift the currents
            Default: False, since the currents are recalculated from
            scratch at each PIC cycle
        """
        if grid.use_cuda:
            # Shift all the fields on the GPU
            grid.Er = self.shift_interp_field_gpu( grid.Er, n_move )
            grid.Et = self.shift_interp_field_gpu( grid.Et, n_move )
            grid.Ez = self.shift_interp_field_gpu( grid.Ez, n_move )
            grid.Br = self.shift_interp_field_gpu( grid.Br, n_move )
            grid.Bt = self.shift_interp_field_gpu( grid.Bt, n_move )
            grid.Bz = self.shift_interp_field_gpu( grid.Bz, n_move )
            if shift_rho:
                grid.rho = self.shift_interp_field_gpu( grid.rho, n_move )
            if shift_currents:
                grid.Jr = self.shift_interp_field_gpu( grid.Jr, n_move )
                grid.Jt = self.shift_interp_field_gpu( grid.Jt, n_move )
                grid.Jz = self.shift_interp_field_gpu( grid.Jz, n_move )
        else:
            # Shift all the fields on the CPU
            self.shift_interp_field( grid.Er, n_move )
            self.shift_interp_field( grid.Et, n_move )
            self.shift_interp_field( grid.Ez, n_move )
            self.shift_interp_field( grid.Br, n_move )
            self.shift_interp_field( grid.Bt, n_move )
            self.shift_interp_field( grid.Bz, n_move )
            if shift_rho:
                self.shift_interp_field( grid.rho, n_move )
            if shift_currents:
                self.shift_interp_field( grid.Jr, n_move )
                self.shift_interp_field( grid.Jt, n_move )
                self.shift_interp_field( grid.Jz, n_move )

    def shift_interp_field( self, field_array, n_move ):
        """
        Shift the field 'field_array' by n_move cells.
        (Typically n_move is positive, and the fields are shifted backwards)

        Parameters
        ----------
        field_array: 2darray of complexs
            Contains the value of the fields, and is modified by
            this function

        n_move: int
            The number of cells by which the grid should be shifted
        """
        if n_move > 0:
            # Transfer the values to n_move cell before
            field_array[:-n_move,:] = field_array[n_move:,:]
            # Put the last cells to 0
            field_array[-n_move:,:] = 0
        if n_move < 0:
            # Transfer the values to n_move cell before
            field_array[abs(n_move):,:] = field_array[:-abs(n_move),:]
            # Put the last cells to 0
            field_array[:abs(n_move),:] = 0

    def shift_interp_field_gpu( self, field_array, n_move):
        """
        Shift the field 'field_array' by n_move cells
        on the GPU by applying a kernel that copies the shifted
        fields to a buffer array. (Typically n_move is positive, and the
        fields are shifted backwards)

        Parameters
        ----------
        field_array: 2darray of complexs
            Contains the value of the fields, and is modified by
            this function

        n_move: int
            The number of cells by which the grid should be shifted

        Returns
        -------
        The new shifted field array
        """
        # Get a 2D CUDA grid of the size of the grid
        dim_grid_2d, dim_block_2d = cuda_tpb_bpg_2d(
            field_array.shape[0], field_array.shape[1] )
        # Initialize a field buffer to temporarily store the data
        field_buffer = cuda.device_array(
            (field_array.shape[0], field_array.shape[1]), dtype=np.complex128)
        # Shift the field array and copy it to the buffer
        shift_interp_array_gpu[dim_grid_2d, dim_block_2d](
            field_array, field_buffer, n_move)
        # Assign the buffer to the original field array object
        field_array = field_buffer
        # Return the new shifted field array
        return( field_array )

if cuda_installed:

    @cuda.jit('void(complex128[:,:], complex128[:,:], int32)')
    def shift_interp_array_gpu( field_array, field_buffer, n_move ):
        """
        Shift a field array by reading the values from the field_array
        and writing them to the field_buffer on the GPU.

        Parameters:
        ------------
        field_array, field_buffer: 2darrays of complexs
            Contains the unshifted field (field_array)
            Contains the shifted field (field_buffer) afterwards

        n_move: int
            Amount of cells by which the field array should be
            shifted in the longitudinal direction.
        """
        # Get a 2D CUDA grid
        iz, ir = cuda.grid(2)

        # Only access values that are actually in the array
        if ir < field_array.shape[1] and iz < field_array.shape[0]:

            # Shift the values of the field array and copy them to the buffer
            if (iz+n_move) < field_array.shape[0] and (iz+n_move) >= 0 :
                field_buffer[iz, ir] = field_array[iz+n_move, ir]

            # Set the remaining values to zero
            if (iz+n_move) >= field_array.shape[0] or (iz+n_move) < 0:
                field_buffer[iz, ir] = 0.

    @cuda.jit('void(complex128[:,:], complex128[:], int32)')
    def shift_spect_array_gpu( field_array, shift_factor, n_move ):
        """
        Shift the field 'field_array' by n_move cells on the GPU.
        This is done in spectral space and corresponds to multiplying the
        fields with the factor exp(i*kz_true*dz)**n_move .

        Parameters
        ----------
        field_array: 2darray of complexs
            Contains the value of the fields, and is modified by
            this function

        shift_factor: 1darray of complexs
            Contains the shift array, that is multiplied to the fields in
            spectral space to shift them by one cell in spatial space
            ( exp(i*kz_true*dz) )

        n_move: int
            The number of cells by which the grid should be shifted
        """
        # Get a 2D CUDA grid
        iz, ir = cuda.grid(2)

        # Only access values that are actually in the array
        if ir < field_array.shape[1] and iz < field_array.shape[0]:
            # Calculate the shift factor (raising to the power n_move)
            power_shift = shift_factor[iz]
            for i in range(1,n_move):
                power_shift *= shift_factor[iz]
            # Shift fields backwards
            field_array[iz, ir] *= power_shift
