import numpy as np
import openmdao.api as om
from dymos.models.atmosphere.atmos_1976 import USatm1976Comp

from aviary.constants import RHO_SEA_LEVEL_ENGLISH as rho_sl
from aviary.mission.gasp_based.ode.base_ode import BaseODE
from aviary.mission.gasp_based.ode.params import ParamPort
from aviary.mission.gasp_based.ode.unsteady_solved.gamma_comp import GammaComp
from aviary.mission.gasp_based.ode.unsteady_solved.unsteady_solved_flight_conditions import \
    UnsteadySolvedFlightConditions
from aviary.mission.gasp_based.ode.unsteady_solved.unsteady_solved_eom import UnsteadySolvedEOM
from aviary.variable_info.enums import SpeedType
from aviary.variable_info.variables import Dynamic
from aviary.variable_info.variables_in import VariablesIn
from aviary.subsystems.aerodynamics.aerodynamics_builder import AerodynamicsBuilderBase
from aviary.subsystems.propulsion.propulsion_builder import PropulsionBuilderBase


class UnsteadySolvedODE(BaseODE):
    """ This 2D aircraft ODE provides the rate of change of time per unit range covered.

    Altitude and velocity at various points along the trajectory are provided, along with their corresponding
    rates of change. The latter are automatically generated by differentiating the interpolating polynomials
    fit to the values. Range is the integration variable for this ODE.

    For the given altitude/velocity trajectory, this ODE then provides the alpha and thrust history that make that
    given trajectory physically realizable.

    Thrust is allowed to take on physically-nonsensical values (negative, or extremely large magnitudes) to provide
    robust convergence of the nonlinear solver. This discrepancy is then resolved by imposing a path constraint that the
    thrust needed to perform the trajectory equals the thrust generated by the propulsion system with its given
    settings.
    """

    def initialize(self):
        super().initialize()
        self.options.declare(
            "ground_roll",
            types=bool,
            default=False,
            desc="True if the aircraft is confined to the ground. Removes altitude rate as an "
            "output and adjusts the TAS rate equation.")
        self.options.declare(
            "clean",
            types=bool,
            default=False,
            desc="If true then no flaps or gear are included. Useful for high-speed flight phases.")
        self.options.declare(
            "include_param_comp",
            types=bool,
            default=True,
            desc="If true then add a ParamComp to this ODE. Useful for smaller usages of this ODE not within a full trajectory or a static analysis group.")
        self.options.declare(
            "input_speed_type",
            default=SpeedType.TAS,
            types=SpeedType,
            desc="Airspeed type specified as input.")
        self.options.declare(
            'balance_throttle',
            types=bool,
            default=False,
            desc='Flag if throttle should be solved for to match thrust to drag'
        )

    def setup(self):
        nn = self.options["num_nodes"]
        ground_roll = self.options["ground_roll"]
        input_speed_type = self.options["input_speed_type"]
        aviary_options = self.options['aviary_options']
        subsystem_options = self.options['subsystem_options']
        core_subsystems = self.options['core_subsystems']
        balance_throttle = self.options['balance_throttle']

        if self.options["include_param_comp"]:
            # TODO: paramport
            self.add_subsystem("params", ParamPort(), promotes=["*"])

            self.add_subsystem(
                'input_port',
                VariablesIn(aviary_options=aviary_options),
                promotes_inputs=['*'],
                promotes_outputs=['*'])

        self.add_subsystem(
            "USatm",
            USatm1976Comp(
                num_nodes=nn),
            promotes_inputs=[
                ("h",
                 Dynamic.Mission.ALTITUDE)],
            promotes_outputs=[
                "rho",
                ("sos",
                 Dynamic.Mission.SPEED_OF_SOUND),
                ("temp",
                 Dynamic.Mission.TEMPERATURE),
                ("pres",
                 Dynamic.Mission.STATIC_PRESSURE),
                "viscosity",
                "drhos_dh"],
        )

        self.add_subsystem("flight_path_angle",
                           GammaComp(num_nodes=nn),
                           promotes_inputs=["*"],
                           promotes_outputs=["*"])

        self.add_subsystem(
            "fc",
            UnsteadySolvedFlightConditions(num_nodes=nn,
                                           ground_roll=ground_roll,
                                           input_speed_type=input_speed_type),
            promotes_inputs=["*"],
            promotes_outputs=["*"],
        )

        control_iter_group = self.add_subsystem("control_iter_group",
                                                subsys=om.Group(),
                                                promotes_inputs=["*"],
                                                promotes_outputs=["*"])

        # Also need to change the run script and the iter group solver when using this; just testing for now
        if balance_throttle:
            throttle_balance_group = self.add_subsystem("throttle_balance_group",
                                                        om.Group(),
                                                        promotes=["*"])

            throttle_balance_comp = om.BalanceComp()
            throttle_balance_comp.add_balance(Dynamic.Mission.THROTTLE,
                                              units="unitless",
                                              val=np.ones(nn) * 0.5,
                                              lhs_name=Dynamic.Mission.THRUST_TOTAL,
                                              rhs_name="thrust_req",
                                              eq_units="lbf",
                                              normalize=True,
                                              lower=0.0,
                                              upper=1.0,
                                              )

            throttle_balance_group.add_subsystem("throttle_balance_comp", subsys=throttle_balance_comp,
                                                 promotes_inputs=["*"],
                                                 promotes_outputs=["*"])

            throttle_balance_group.nonlinear_solver = om.NewtonSolver(solve_subsystems=True,
                                                                      atol=1.0e-10,
                                                                      rtol=1.0e-10,
                                                                      )
            throttle_balance_group.nonlinear_solver.linesearch = om.BoundsEnforceLS()
            throttle_balance_group.linear_solver = om.DirectSolver(assemble_jac=True)
            throttle_balance_group.nonlinear_solver.options['err_on_non_converge'] = True

        kwargs = {'num_nodes': nn, 'aviary_inputs': aviary_options,
                  'method': 'low_speed'}
        if self.options['clean']:
            kwargs['method'] = 'cruise'
            kwargs['output_alpha'] = False
        for subsystem in core_subsystems:
            system = subsystem.build_mission(**kwargs)
            if system is not None:
                if isinstance(subsystem, AerodynamicsBuilderBase):
                    control_iter_group.add_subsystem(subsystem.name,
                                                     system,
                                                     promotes_inputs=subsystem.mission_inputs(
                                                         **kwargs),
                                                     promotes_outputs=subsystem.mission_outputs(**kwargs))
                elif isinstance(subsystem, PropulsionBuilderBase) and balance_throttle:
                    throttle_balance_group.add_subsystem(subsystem.name,
                                                         system,
                                                         promotes_inputs=subsystem.mission_inputs(
                                                             **kwargs),
                                                         promotes_outputs=subsystem.mission_outputs(**kwargs))
                else:
                    self.add_subsystem(subsystem.name,
                                       system,
                                       promotes_inputs=subsystem.mission_inputs(
                                           **kwargs),
                                       promotes_outputs=subsystem.mission_outputs(**kwargs))

        eom_comp = UnsteadySolvedEOM(num_nodes=nn, ground_roll=ground_roll)

        control_iter_group.add_subsystem("eom", subsys=eom_comp,
                                         promotes_inputs=["*",
                                                          (Dynamic.Mission.THRUST_TOTAL, "thrust_req")],
                                         promotes_outputs=["*"])

        thrust_alpha_bal = om.BalanceComp()
        if not self.options['ground_roll']:
            thrust_alpha_bal.add_balance("alpha",
                                         units="rad",
                                         val=np.zeros(nn),
                                         lhs_name="dgam_dt_approx",
                                         rhs_name="dgam_dt",
                                         eq_units="rad/s",
                                         normalize=False)

        thrust_alpha_bal.add_balance("thrust_req",
                                     units="N",
                                     val=100*np.ones(nn),
                                     lhs_name="dTAS_dt_approx",
                                     rhs_name="dTAS_dt",
                                     eq_units="m/s**2",
                                     normalize=False)

        control_iter_group.add_subsystem("thrust_alpha_bal", subsys=thrust_alpha_bal,
                                         promotes_inputs=["*"],
                                         promotes_outputs=["*"])

        control_iter_group.nonlinear_solver = om.NewtonSolver(solve_subsystems=True,
                                                              atol=1.0e-10,
                                                              rtol=1.0e-10)
        # self.nonlinear_solver.linesearch = om.ArmijoGoldsteinLS()
        control_iter_group.linear_solver = om.DirectSolver(assemble_jac=True)

        self.add_subsystem("mass_rate",
                           om.ExecComp("dmass_dr = fuelflow * dt_dr",
                                       fuelflow={"units": "lbm/s", "shape": nn},
                                       dt_dr={"units": "s/range_units", "shape": nn},
                                       dmass_dr={"units": "lbm/range_units",
                                                 "shape": nn,
                                                 "tags": ['dymos.state_rate_source:mass',
                                                          'dymos.state_units:lbm']},
                                       has_diag_partials=True),
                           promotes_inputs=[
                               ("fuelflow", Dynamic.Mission.FUEL_FLOW_RATE_NEGATIVE_TOTAL), "dt_dr"],
                           promotes_outputs=["dmass_dr"])

        if self.options["include_param_comp"]:
            ParamPort.set_default_vals(self)

        onn = np.ones(nn)
        self.set_input_defaults(name="rho", val=rho_sl * onn, units="slug/ft**3")
        self.set_input_defaults(
            name=Dynamic.Mission.SPEED_OF_SOUND,
            val=1116.4 * onn,
            units="ft/s")
        if not self.options['ground_roll']:
            self.set_input_defaults(
                name=Dynamic.Mission.FLIGHT_PATH_ANGLE, val=0.0 * onn, units="rad")
        self.set_input_defaults(name="TAS", val=250. * onn, units="kn")
        self.set_input_defaults(
            name=Dynamic.Mission.ALTITUDE,
            val=10000. * onn,
            units="ft")
        self.set_input_defaults(name="dh_dr", val=0. * onn, units="ft/range_units")
        self.set_input_defaults(name="d2h_dr2", val=0. * onn, units="ft/range_units**2")