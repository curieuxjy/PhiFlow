from .domain import *
from phi.math import *
from operator import itemgetter
import itertools


def initialize_field(value, shape):
    if isinstance(value, (int, float)):
        return zeros(shape) + value
    elif callable(value):
        return value(shape)
    if isinstance(shape, Struct):
        if type(shape) == type(value):
            return Struct.zippedmap(lambda val, sh: initialize_field(val, sh), value, shape)
        else:
            return type(shape)(value)
    else:
        return value


def domain(state, obstacles):
    if state.domaincache is None or not state.domaincache.is_valid(obstacles):
        mask = 1 - geometry_mask([o.geometry for o in obstacles], state.grid)
        if state.domaincache is None:
            active_mask = mask
        else:
            active_mask = mask * state.domaincache.active()
        state.domaincache = DomainCache(state.domain, obstacles, active=active_mask, accessible=mask)
    return state.domaincache


class GridLiquidPhysics(Physics):

    def __init__(self, pressure_solver=None):
        Physics.__init__(self, {'obstacles': ['obstacle'], 'inflows': 'inflow'})
        self.pressure_solver = pressure_solver

    def step(self, state, dt=1.0, obstacles=(), inflows=(), **dependent_states):
        assert len(dependent_states) == 0
        domaincache = domain(state, obstacles)
        # step
        inflow_density = dt * inflow(inflows, state.grid)
        density = state.density + inflow_density
        # Update the active mask based on the new fluid-filled grid cells (for pressure solve)
        active_mask = create_binary_mask(density, threshold=0.5)
        domaincache._active = active_mask

        forces = dt * state.gravity
        velocity = state.velocity + forces

        velocity = divergence_free(velocity, domaincache, self.pressure_solver, state=state)

        #max_vel = math.max(math.abs(velocity.staggered))
        _, ext_velocity = extrapolate(velocity, domaincache.active(), dx=1.0, distance=30)
        ext_velocity = domaincache.with_hard_boundary_conditions(ext_velocity)

        density = ext_velocity.advect(density, dt=dt)
        velocity = ext_velocity.advect(ext_velocity, dt=dt)
        
        return state.copied_with(density=density, velocity=velocity, age=state.age + dt)


GRIDLIQUID = GridLiquidPhysics()


class GridLiquid(State):
    __struct__ = State.__struct__.extend(('_density', '_velocity'),
                            ('_domain', '_gravity'))

    def __init__(self, domain=Open2D,
                 density=0.0, velocity=zeros, gravity=-9.81, batch_size=None):
        State.__init__(self, tags=('liquid', 'velocityfield'), batch_size=batch_size)
        self._domain = domain
        self._density = density
        self._velocity = velocity
        self.domaincache = None
        self._last_pressure = None
        self._last_pressure_iterations = None

        if isinstance(gravity, (tuple, list)):
            assert len(gravity) == domain.rank
            self._gravity = np.array(gravity)
        elif domain.rank == 1:
            self._gravity = np.array([gravity])
        else:
            assert domain.rank >= 2
            gravity = ([0] * (domain.rank - 2)) + [gravity] + [0]
            self._gravity = np.array(gravity)

    def default_physics(self):
        return GRIDLIQUID

    @property
    def density(self):
        return self._density

    @property
    def _density(self):
        return self._density_field

    @_density.setter
    def _density(self, value):
        self._density_field = initialize_field(value, self.grid.shape())

    @property
    def velocity(self):
        return self._velocity

    @property
    def _velocity(self):
        return self._velocity_field

    @_velocity.setter
    def _velocity(self, value):
        self._velocity_field = initialize_field(value, self.grid.staggered_shape())

    @property
    def domain(self):
        return self._domain

    @property
    def grid(self):
        return self.domain.grid

    @property
    def rank(self):
        return self.grid.rank

    @property
    def gravity(self):
        return self._gravity

    @property
    def last_pressure(self):
        return self._last_pressure

    @property
    def last_pressure_iterations(self):
        return self._last_pressure_iterations

    def __repr__(self):
        return "Liquid[density: %s, velocity: %s]" % (self.density, self.velocity)

    def __add__(self, other):
        if isinstance(other, StaggeredGrid):
            return self.copied_with(velocity=self.velocity + other)
        else:
            return self.copied_with(density=self.density + other)

    def __sub__(self, other):
        if isinstance(other, StaggeredGrid):
            return self.copied_with(velocity=self.velocity - other)
        else:
            return self.copied_with(density=self.density - other)



def solve_pressure(input_field, domaincache, pressure_solver=None):
    """
Calculates the pressure from the given velocity or velocity divergence using the specified solver.
    :param obj: tensor containing the centered velocity divergence values or velocity as StaggeredGrid
    :param solver: PressureSolver to use, options DEFAULT, SCIPY or MANTA
    :return: scalar pressure channel as tensor
    """
    if isinstance(input_field, State):
        div = input_field.velocity.divergence()
    elif isinstance(input_field, StaggeredGrid):
        div = input_field.divergence()
    elif input_field.shape[-1] == domaincache.rank:
        div = nd.divergence(input_field, difference='central')
    else:
        raise ValueError("Cannot solve pressure for %s" % input_field)

    if pressure_solver is None:
        from phi.solver.sparse import SparseCG
        pressure_solver = SparseCG()

    #div = div * domaincache.active()

    pressure, iter = pressure_solver.solve(div, domaincache, pressure_guess=None)
    return pressure, iter


def divergence_free(obj, domaincache, pressure_solver=None, state=None):
    if isinstance(obj, State):
        # of course only works if the State has a velocity component
        return obj.copied_with(velocity=divergence_free(obj.velocity, domaincache))
    assert isinstance(obj, StaggeredGrid)
    velocity = obj

    _, ext_velocity = extrapolate(velocity, domaincache.active(), dx=1.0, distance=2)
    ext_velocity = domaincache.with_hard_boundary_conditions(ext_velocity)
    
    pressure, iter = solve_pressure(ext_velocity, domaincache, pressure_solver)
    gradp = StaggeredGrid.gradient(pressure)
    # No need to multiply with dt here because we didn't divide divergence by dt in pressure solve.
    velocity = domaincache.with_hard_boundary_conditions(velocity - gradp)
    if state is not None:
        state._last_pressure = pressure
        state._last_pressure_iterations = iter
    return velocity


def inflow(inflows, grid):
    if len(inflows) == 0:
        return zeros(grid.shape())
    location = grid.center_points()
    return add([inflow.geometry.value_at(location) * inflow.rate for inflow in inflows])


def stick(velocity, domaincache, dt):
    velocity = domaincache.with_hard_boundary_conditions(velocity)
    # TODO wall friction
    # self.world.geom
    # friction = material.friction_multiplier(dt)
    return velocity


def create_binary_mask(field, threshold=1e-5):
    """
Builds a binary tensor with the same shape as field. Wherever field is greater than threshold, the binary mask will contain a '1', else the entry will be '0'.
    :param threshold: Optional scalar value. Threshold relative to the maximal value in the field, must be between 0 and 1. Default is 1e-5.
    :return: The binary mask according to the given input field.
    """
    if isinstance(field, StaggeredGrid):
        field = field.staggered
    f_max = math.max(math.abs(field))
    scaled_field = (math.abs(field) / f_max) if f_max != 0 else (0 * field)
    binary_mask = math.ceil(scaled_field - threshold)

    return binary_mask


def create_surface_mask(particle_mask):
    # When we create inner contour, we don't want the fluid-wall boundaries to show up as surface, so we should pad with symmetric edge values.
    mask = math.pad(particle_mask, [[0, 0]] + [[1, 1]] * spatial_rank(particle_mask) + [[0, 0]], "symmetric")
    dims = range(spatial_rank(mask))
    bcs = math.zeros_like(particle_mask)
    for d in dims:
        upper_slices = [(slice(2, None) if i == d else slice(1, -1)) for i in dims]
        center_slices = [slice(1, -1) for _ in dims]
        lower_slices = [(slice(0, -2) if i == d else slice(1, -1)) for i in dims]
        
        # Create inner contour of particles
        bc_d = math.maximum (mask[[slice(None)] + upper_slices + [slice(None)]],
                                mask[[slice(None)] + center_slices + [slice(None)]]) - \
                            mask[[slice(None)] + upper_slices + [slice(None)]]
        bcs = math.maximum (bcs, bc_d)
        
        bc_d = math.maximum (mask[[slice(None)] + center_slices + [slice(None)]],
                                mask[[slice(None)] + lower_slices + [slice(None)]]) - \
                            mask[[slice(None)] + lower_slices + [slice(None)]]
        bcs = math.maximum (bcs, bc_d)
    return bcs


def extrapolate(input_field, particle_mask, dx=1.0, distance=10):
    """
Create a signed distance field for the grid, where negative signs are fluid cells and positive signs are empty cells. The fluid surface is located at the points where the interpolated value is zero. Then extrapolate the input field into the air cells.
    :param input_field: Field to be extrapolated
    :param particle_mask: One dimensional binary mask indicating where fluid is present
    :param dx: Optional grid cells width
    :param distance: Optional maximal distance (in number of grid cells) where signed distance should still be calculated / how far should be extrapolated.
    """
    ext_field = 1. * input_field    # Copy the original field, so we don't edit it.
    if isinstance(input_field, StaggeredGrid):
        ext_field = input_field.staggered
        particle_mask = math.pad(particle_mask, [[0,0]] + [[0,1]] * spatial_rank(input_field) + [[0,0]], "constant")

    dims = range(spatial_rank(input_field))
    # Larger than distance to be safe. It could start extrapolating velocities from outside distance into the field.
    s_distance = -2.0 * (distance+1) * (2*particle_mask - 1)
    signs = -1 * (2*particle_mask - 1)

    surface_mask = create_surface_mask(particle_mask)
    # surface_mask == 1 doesn't output a tensor, just a scalar, but >= works.
    # Initialize the distance with 0 at the surface
    s_distance = math.where((surface_mask >= 1), 0.0 * math.ones_like(s_distance), s_distance)
    
        
    directions = np.array(list(itertools.product(
        *np.tile( (-1,0,1) , (len(dims),1) )
        )))

    # First make a move in every positive direction (StaggeredGrid velocities there are correct, we want to extrapolate these)
    if isinstance(input_field, StaggeredGrid):
        for d in directions:
            if (d <= 0).all():
                    continue
                    
            # Shift the field in direction d, compare new distances to old ones.
            d_slice = [(slice(1, None) if d[i] == -1 else slice(0,-1) if d[i] == 1 else slice(None)) for i in dims]

            d_field = math.pad(ext_field, [[0,0]] + [([0,1] if d[i] == -1 else [1,0] if d[i] == 1 else [0,0]) for i in dims] + [[0,0]], "symmetric")
            d_field = d_field[[slice(None)] + d_slice + [slice(None)]]

            d_dist = math.pad(s_distance, [[0,0]] + [([0,1] if d[i] == -1 else [1,0] if d[i] == 1 else [0,0]) for i in dims] + [[0,0]], "symmetric")
            d_dist = d_dist[[slice(None)] + d_slice + [slice(None)]]
            d_dist += dx * np.sqrt(d.dot(d)) * signs


            if (d.dot(d) == 1) and (d >= 0).all():
                # Pure axis direction (1,0,0), (0,1,0), (0,0,1)
                updates = (math.abs(d_dist) < math.abs(s_distance)) & (signs >= 0)
                ext_field = math.where(math.concat([(math.zeros_like(updates) if d[i] == 1 else updates) for i in dims], axis=-1), d_field, ext_field)
                s_distance = math.where(updates, d_dist, s_distance)
            else:
                # Mixed axis direction (1,1,0), (1,1,-1), etc.
                continue


    for _ in range(distance):
        for d in directions:
            if (d==0).all():
                continue
                
            # Shift the field in direction d, compare new distances to old ones.
            d_slice = [(slice(1, None) if d[i] == -1 else slice(0,-1) if d[i] == 1 else slice(None)) for i in dims]

            d_field = math.pad(ext_field, [[0,0]] + [([0,1] if d[i] == -1 else [1,0] if d[i] == 1 else [0,0]) for i in dims] + [[0,0]], "symmetric")
            d_field = d_field[[slice(None)] + d_slice + [slice(None)]]

            d_dist = math.pad(s_distance, [[0,0]] + [([0,1] if d[i] == -1 else [1,0] if d[i] == 1 else [0,0]) for i in dims] + [[0,0]], "symmetric")
            d_dist = d_dist[[slice(None)] + d_slice + [slice(None)]]
            d_dist += dx * np.sqrt(d.dot(d)) * signs

            # TODO: we also want negative distance inside fluid
            updates = (math.abs(d_dist) < math.abs(s_distance)) & (signs >= 0)
            ext_field = math.where(math.concat([updates] * spatial_rank(ext_field), axis=-1), d_field, ext_field)
            s_distance = math.where(updates, d_dist, s_distance)
            
    if isinstance(input_field, StaggeredGrid):
        ext_field = StaggeredGrid(ext_field)
        stagger_slice = [slice(0,-1) for i in dims]
        s_distance = s_distance[[slice(None)] + stagger_slice + [slice(None)]]

    return s_distance, ext_field